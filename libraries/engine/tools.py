"""🌸 engine/tools.py — Groq custom Tools / Function Calling for the main
chat model (engine/completion.py).

Replaces the old "separate cheap AI call guesses an intent label BEFORE
the real chat call" architecture (classify_math_request, the media /
music / server-query classifiers in groq_service.py's interceptor chain).
Now the MAIN model decides, in the same pass that writes the reply,
whether to call one of these tools and fills in the JSON arguments
itself:

    calculate_local_math      -> extras.groq_math.handle_math_request
    search_visual_media       -> groq_pexels.handle_media_request
    search_and_play_music     -> groq_music_suggestion.handle_music_request
    query_peran_server        -> groq_instruct.handle_role_query
    check_member_existence    -> groq_instruct.handle_member_lookup_query
                                 (+ intent='props': facts/jokes/roasts from real member props,
                                  + intent='profile': pfp/banner description)

Division of labor (so completion.py stays small):
  - TOOL_SCHEMAS / get_tool_schemas()  → what _call() puts in kwargs["tools"]
  - assistant_tool_call_message()      → turns Groq's response message into
                                         the dict that goes back into `messages`
  - run_tool_calls()                   → executes every tool call in one model
                                         response and returns the role="tool"
                                         messages (one per tool_call_id — Groq
                                         400s if any call is left unanswered,
                                         so this function NEVER raises).

Handler imports are lazy (inside each executor) because groq_service.py
imports engine.* at module load — importing it back at the top of this
file would be circular.
"""
import json
import re
import asyncio
import inspect
import random
from collections import OrderedDict
from datetime import datetime, timezone

import discord

# 🌸 Hard cap on model→tool→model round-trips per reply. Each round costs
# a full Groq call, and a model that keeps calling tools is almost always
# stuck. After this many rounds completion.py flips tool_choice to "none"
# so the model has to answer in words.
MAX_TOOL_ROUNDS = 3

# 🌸 Per-tool wall-clock cap. Handlers hit external APIs (Pexels, Pixabay,
# YouTube Music) — a hung one must not hold the typing indicator forever.
TOOL_TIMEOUT_SECONDS = 45

# 🌸 Two result styles (see the executors below):
#   - search_visual_media / search_and_play_music return things the model
#     CAN'T put into words (an image embed, a music card with buttons), so
#     the executor sends them to the channel by code via _deliver() and the
#     model only gets a short stub to write its reaction around.
#   - calculate_local_math / query_peran_server / check_member_existence
#     return TEXT that goes straight back to the model, which phrases the
#     final answer itself. To make a server lookup post its embed instead,
#     have its executor call _deliver() rather than _text_result().

# 🌸 Tools that only make sense inside a server (they read the guild's
# cached roles/members). Dropped from the schema list in DMs so the model
# can't even try them.
GUILD_ONLY_TOOLS = frozenset({"query_peran_server", "check_member_existence", "send_dm_to_user"})

# 🌸 Same ping policy as groq_service.SAFE_REPLY_MENTIONS (pings only the
# person being replied to). Duplicated here instead of imported to avoid
# the circular import noted above.
_SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=True)

# 🌸 Cap on how much tool-result text is fed back to the model (embed
# text from role/member lookups can be long). Keeps the follow-up call
# cheap — this bot lives on thin TPM budgets.
_MAX_TOOL_RESULT_CHARS = 1800

# 🌸 check_member_existence(explicit pfp/banner questions only): the main chat model (gpt-oss) is
# text-only, so a member's profile picture / banner is first described by ONE
# small vision call and that description goes into the tool result. Set this
# to whichever vision-capable model you want (a model that doesn't accept
# images just makes the call fail — the lookup then degrades to "couldn't
# view the images", it never crashes).
PROFILE_VISION_MODEL = "qwen/qwen3.8-27b"
_PROFILE_VISION_TIMEOUT = 25  # must stay well under TOOL_TIMEOUT_SECONDS
_PROFILE_VISION_MAX_TOKENS = 700  # qwen may spend tokens on <think> first

# 🌸 (avatar_url, banner_url) -> {"avatar": ..., "banner": ...}. Discord CDN
# URLs contain the image hash, so a changed pfp/banner is a new key and a
# stale description can never be served. Bounded: thin TPM budget, and the
# same member is often asked about repeatedly.
_PROFILE_DESC_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_PROFILE_DESC_CACHE_MAX = 100

# 🌸 check_member_existence(intent='props'): the member's real server
# properties (account age, join date, roles, badges…) for facts / jokes /
# light roasts / trivia. Pure discord.py cache reads — no extra API call.
_PROPS_MAX_ROLES = 12          # role names listed (highest first)
_PROPS_MAX_CHARS = 750         # hard cap on the props block
_MEMBER_RESULT_MAX_CHARS = 2600  # member lookups may carry profile + props


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the live web (Exa) and get back a short, already-summarized "
                "answer. Call this for ANYTHING that depends on current or real-world "
                "information you can't know reliably: news, scores, prices, weather, "
                "releases, 'latest', 'right now', 'who is the current…', recent events, "
                "or facts you aren't sure of. NOT for chit-chat, opinions, math, or "
                "questions about this bot, this server or its members (use the server "
                "tools for those)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A clear, self-contained search query in English (or the "
                            "user's language for local topics). Include names and "
                            "context from the conversation — it is searched on its own."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Optional. One of: news, research paper, company, financial "
                            "report, github, pdf, tweet, personal site, linkedin profile, "
                            "people. Omit it for general questions."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "react_to_mood",
            "description": (
                "Add a matching emoji reaction to the user's message and get tone "
                "guidance for your reply. Call this AT MOST ONCE, and ONLY when the "
                "user's mood is clearly expressed: upset/sad, stressed/anxious, "
                "angry/frustrated, or excited/hyped. Do NOT call it for neutral, "
                "jokey or purely informational messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {
                        "type": "string",
                        "description": "Exactly one of: upset, stressed, angry, excited.",
                    },
                },
                "required": ["mood"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_local_math",
            "description": (
                "Evaluate a math expression EXACTLY with a deterministic calculator. "
                "Call this whenever the user wants a number computed (arithmetic, "
                "percentages, powers, roots, trig, factorials, etc.) — never compute "
                "it yourself, LLM arithmetic is unreliable. Do NOT call it for dates, "
                "IDs, or casual mentions of a number with nothing to compute."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "The raw math expression in Python syntax: * multiply, "
                            "** power, sqrt(), sin(), pi, etc. Example: '9*8*9*9', "
                            "'sqrt(144)', '15% of 80' -> '0.15*80'. Leave out chatter "
                            "like 'send it to my dm'."
                        ),
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_visual_media",
            "description": (
                "Find and post a real image or video in the channel. Call this when the "
                "user asks you to send/show/find a picture, photo, video, clip, vector, "
                "cartoon, illustration or anime/manga art of something. NOT for songs, "
                "music or audio — use search_and_play_music for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Short English search subject, 1-5 words, no filler "
                            "(e.g. 'red panda', 'sunset beach', 'anime girl')."
                        ),
                    },
                    # 🌸 Deliberately NOT a strict JSON-schema enum: Groq validates
                    # tool arguments server-side, and a model that wrote
                    # kind="anime" got the WHOLE call rejected (400
                    # tool_use_failed) instead of just being mapped to the
                    # right bucket. The four allowed values are spelled out
                    # in the description and _exec_media normalizes aliases.
                    "kind": {
                        "type": "string",
                        "description": (
                            "Exactly one of: photo (real photograph), video (real "
                            "video clip), vector (flat vector graphic), cartoon "
                            "(drawn/illustrated art — use this for anime and manga)."
                        ),
                    },
                },
                "required": ["query", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_and_play_music",
            "description": (
                "Search YouTube Music and post a playable song card with links. Call this "
                "when the user asks for a song, track, artist, playlist or music "
                "recommendation. NOT for images or videos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Song title and/or artist name to search for, e.g. "
                            "'Bohemian Rhapsody Queen'. For a vague request "
                            "('something chill') pick a concrete well-known song or "
                            "artist that fits."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_peran_server",
            "description": (
                "Look up a ROLE in this Discord server: who holds it and its details. "
                "You have NO role data, so you MUST call this for any question like 'who has "
                "the Moderator role' or 'who are the members of role X' — never list names "
                "from your own guess. Server-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role_name": {
                        "type": "string",
                        "description": (
                            "The name of the role being searched for, without the @ — "
                            "OR the role's Discord ID / a <@&ID> mention, exactly as written."
                        ),
                    },
                },
                "required": ["role_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_dm_to_user",
            "description": (
                "Send a private message to the user's DMs. Call this when the user "
                "explicitly asks to be DM'd ('dm me that', 'kirim ke dm dong', 'send it "
                "privately') or when what you'd share is genuinely sensitive (a secret, "
                "coupon/redeem code, password, personal results) and shouldn't be visible "
                "to the whole server. Rarely needed. Server-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "ONLY the private text to deliver, written out in full. It "
                            "must not also appear in your public reply."
                        ),
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decode_discord_snowflake",
            "description": (
                "Decode a Discord ID (snowflake) into the exact date and time it was "
                "created — works for user, server, channel or message IDs. Call this "
                "when the user gives an ID and asks when it was created / how old the "
                "account, server or message is. IDs are public; no lookup is needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "snowflake": {
                        "type": "string",
                        "description": "The 17-20 digit Discord ID, digits only (strip any <@ > wrapper).",
                    },
                },
                "required": ["snowflake"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_member_existence",
            "description": (
                "Look up a person in this Discord server. You have NO member list, so you "
                "MUST call this for ANY question about whether someone is here/there/in/on "
                "this server — however casually or typo'd it is phrased ('is dark side here in "
                "this ssrver', 'is X there'). Never answer yes or no from your own guess, and "
                "call it AGAIN even if the same person was already asked about earlier in the "
                "chat (membership changes, old answers are stale). "
                "ALSO call it for ANYTHING else about a specific member — a fun fact, joke, "
                "light roast, compliment, trivia, rating, comparison, 'tell me about X', 'how long has "
                "X been here', 'what roles does X have', an opinion — with intent='props': the result "
                "then includes their REAL server properties (account age, join date, roles, nickname, "
                "booster status, badges, status) and you build the fun/fact/joke ONLY from those. You "
                "still do NOT know their personality or private life, so never invent any. "
                "Pass intent='profile' ONLY when the user EXPLICITLY asks about their profile "
                "picture / pfp / avatar / banner / avatar decoration / accent colour or how they look; "
                "then the result includes a description of those (use 'profile,props' for both). "
                "Never use 'profile' just because someone asked for an opinion. "
                "For the asker's OWN profile ('is my pfp cool') pass the asker's own name — the @mention that "
                "addresses YOU (the bot) is never the target. "
                "NOT for celebrities or public figures who aren't members here. Server-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {
                        "type": "string",
                        "description": (
                            "Name, display name or username of the member to look for "
                            "(may be several words), OR their Discord ID (17-20 digits) "
                            "OR a <@ID> mention — copied exactly as the user wrote it."
                        ),
                    },
                    # 🌸 String on purpose (not enum/boolean) — same reason as
                    # search_visual_media's "kind": Groq rejects the WHOLE call
                    # when a strict-typed argument is slightly off.
                    "intent": {
                        "type": "string",
                        "description": (
                            "Optional. 'props' when the user wants ANYTHING beyond a plain yes/no about "
                            "this member: a fact, joke, light roast, compliment, trivia, rating, comparison, "
                            "opinion or 'tell me about X' — you then get their real server properties "
                            "(account age, join date, roles, nickname, booster, badges, status) to build it from. "
                            "'profile' ONLY when the user explicitly asks about their profile picture, avatar, "
                            "banner, avatar decoration or accent colour, or how they look. 'profile,props' for "
                            "both. Omit it for a plain 'is X here'."
                        ),
                    },
                },
                "required": ["target_name"],
            },
        },
    },
]


# 🌸 Appended to the system prompt ONLY on calls that actually carry
# tools (see _call in completion.py). Without it models tend to paste the
# media link themselves or restate a whole embed after the tool already
# delivered it.
TOOL_USE_DIRECTIVE = (
    "\n\n[Tools] You can call tools. RULES:\n"
    "1. You have NO data about who is in this server or what roles exist. Any question "
    "about whether a person is in/on/here/there in this server, or who has a role, MUST "
    "go through check_member_existence / query_peran_server — never answer yes/no or "
    "name people from your own guess, even if it seems obvious. Call the tool again even "
    "if you answered the same question earlier in this chat. NEVER reply with only an "
    "emoji (a lone check or cross is not an answer): always write a short sentence naming the person. "
    "If the user asks anything about a specific server member (a fact, joke, light roast, "
    "compliment, trivia, rating, 'tell me about X', 'how long has X been here', an opinion), call "
    "check_member_existence with intent='props' and build the answer ONLY from the properties it "
    "returns — you know nothing else about them, so never invent personality, hobbies or private "
    "details; if they want something the properties don't cover, say sweetly you can't see that. "
    "Only when the user EXPLICITLY asks about their profile picture/banner/decoration/accent "
    "colour/looks, pass intent='profile' ('profile,props' for both). "
    "For 'my pfp/banner/accent' (the ASKER's own), target_name is the asker's own name — the "
    "@mention that addresses you is NEVER the target.\n"

    "2. Math: always call calculate_local_math, never compute it yourself, and never "
    "alter a number a tool returned.\n"
    "3. Pictures/videos/anime art → search_visual_media; songs/artists → "
    "search_and_play_music. The card/image is posted automatically right BELOW your "
    "message: write a short, cute, in-character line that leads into it, and don't paste "
    "links or repeat what it contains.\n"
    "4. Private/sensitive content or an explicit 'DM me' → send_dm_to_user (never repeat "
    "that content publicly). A Discord ID someone wants dated → decode_discord_snowflake.\n"
    "5. Normal chat needs no tool — just reply.\n"
    "6. Anything that depends on current/real-world info (news, scores, prices, weather, "
    "'latest', releases, who currently holds a position) → search_web; never answer those "
    "from memory. After a search, summarize in your own words.\n"
    "7. If the user's mood is clearly upset, stressed, angry or excited, call "
    "react_to_mood once and follow the tone guidance it returns; skip it for neutral "
    "or jokey messages.\n"
    "8. If a tool reports an error or nothing found, say so honestly and sweetly instead "
    "of making something up. Never invent links, images, songs or member lists."
)

# 🌸 Appended to the system prompt when a 400 forces a retry WITHOUT tools.
# Without it the model just improvises (it once invented a fake imgur link
# for an image request) — this tells it the honest move.
TOOLS_UNAVAILABLE_NOTE = (
    "\n\n[Note] Your tools (media, music, math, server lookups) are unavailable for this "
    "one reply. If the user's message needed one of them, say sweetly that you couldn't "
    "do it just now and they can try again. Never invent links, images, songs, math "
    "results or member/role information."
)


# 🌸 Models that actually support Groq tool/function calling. Others (e.g.
# qwen/qwen3.8-27b) ignore the tools or answer without calling them — that's
# what produced the bare "✅" replies. When a call carries tools, completion.py
# should pass its model through model_for_tools() so those calls land here.
TOOL_CAPABLE_MODELS = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})
# Swap to "openai/gpt-oss-20b" for a cheaper pick (thin TPM budget); 120b is
# the more reliable tool caller.
TOOL_MODEL = "openai/gpt-oss-120b"


def model_for_tools(model: str, tools_active: bool) -> str:
    """🌸 The model a call should really use. Tool-less calls keep whatever
    was chosen (qwen etc.). A call that carries tools keeps gpt-oss-120b/20b
    if that's what was chosen, otherwise is forced onto TOOL_MODEL. Use it
    for EVERY round of a tool turn, including the follow-up call after the
    tool results come back, so the history stays on one model."""
    if tools_active and model not in TOOL_CAPABLE_MODELS:
        return TOOL_MODEL
    return model


def get_tool_schemas(in_guild: bool, has_search: bool = True) -> list:
    """🌸 The tool list for this call. In a DM the server-only tools are
    left out entirely; search_web is left out when no ExaSearchService
    exists (no key / exa_py missing) so the model never calls a dead tool."""
    tools = list(TOOL_SCHEMAS)
    if not in_guild:
        tools = [t for t in tools if t["function"]["name"] not in GUILD_ONLY_TOOLS]
    if not has_search:
        tools = [t for t in tools if t["function"]["name"] != "search_web"]
    return tools


def assistant_tool_call_message(response_message) -> dict:
    """🌸 Groq's assistant message (the one carrying tool_calls) as a
    plain dict, ready to append to `messages` before the matching
    role="tool" replies. Plain dict instead of the SDK object so it
    serializes the same way every other message in the list does."""
    return {
        "role": "assistant",
        "content": response_message.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in response_message.tool_calls
        ],
    }


# ─────────────────────────────────────────────────────────────────────
# Delivery helpers
# ─────────────────────────────────────────────────────────────────────

def _embed_to_text(embed: discord.Embed) -> str:
    """🌸 Flattens an embed (title/description/fields/footer) into plain
    text so a handler that returns an Embed can still feed the model."""
    parts = []
    if embed.title:
        parts.append(str(embed.title))
    if embed.description:
        parts.append(str(embed.description))
    for field in embed.fields:
        parts.append(f"{field.name}: {field.value}")
    if embed.footer and embed.footer.text:
        parts.append(str(embed.footer.text))
    return "\n".join(parts)


def _with_mention_note(text: str) -> str:
    """🌸 Server lookups now return clickable <@ID> mentions. Tell the model
    to pass them through verbatim (they render as @name chips and, because
    replies are sent with users=False, never ping anyone)."""
    if "<@" in text:
        return text + "\n(Keep every <@id> mention exactly as written in your reply — it shows as a clickable name.)"
    return text


def _clip(text: str, limit: int | None = None) -> str:
    limit = limit or _MAX_TOOL_RESULT_CHARS
    text = (text or "").strip()
    if len(text) > limit:
        return text[:limit] + "\n…(truncated)"
    return text


async def _deliver(ctx: "_Ctx", result) -> str:
    """🌸 QUEUES a handler's return value to be posted in the channel —
    the same shapes groq_service.py's old `if intercepted:` block handled
    — and returns a short stub string for the model.

    Posting is DEFERRED on purpose: the model's own text reply doesn't
    exist yet (it's written AFTER it reads this stub), and the card must
    appear BELOW that text. Each queued send is an async callable stored
    in ctx.pending; run_tool_calls hands the list up, completion.py parks
    it on the service, and groq_service.py flushes it (flush_tool_sends)
    right after the text reply has been sent. The stub tells the model the
    card will land directly below its message so it words things right.

      - (view, tracks, query, summary) tuple → music card (+ persisted
        for the pager buttons via save_music_interaction)
      - discord.Embed                        → embed reply
      - discord.ui.LayoutView                → Components V2 reply
      - str                                  → plain reply (e.g. the bare
                                               .mp4 link, so Discord's own
                                               inline video player kicks in)

    The deferred sends use mention_author=False — the text reply already
    pinged the user once; a second ping for the card would be noise.
    """
    message = ctx.message

    if isinstance(result, tuple):
        music_view, music_tracks, music_query, music_summary = result

        async def _send_music():
            # Lazy: groq_music_suggestion pulls in ytmusicapi/aiosqlite at import.
            from groq_music_suggestion import save_music_interaction

            # 🌸 mention_author=True: the card is now the ONLY reply (the model's
            # text is skipped, see card_delivered), so it carries the ping.
            sent = await message.reply(
                view=music_view, mention_author=True, allowed_mentions=_SAFE_MENTIONS,
            )
            if music_tracks:
                await save_music_interaction(
                    message_id=sent.id,
                    user_id=message.author.id,
                    guild_id=message.guild.id if message.guild else 0,
                    channel_id=message.channel.id,
                    query=music_query,
                    filter_type="songs",
                    tracks=music_tracks,
                    track_index=0,
                    summary=music_summary,
                )

        ctx.pending.append(_send_music)
        ctx.delivered = True
        _mark_card_delivered(message.id)
        if music_tracks:
            top = music_tracks[0]
            return (
                f"A music card with {len(music_tracks)} result(s) for '{music_query}' will be "
                f"posted directly BELOW your message. Top result: {top.get('title', '?')} by "
                f"{top.get('artist', '?')}. Do not paste links."
            )
        return (
            f"Nothing found on YouTube Music for '{music_query}'; a 'couldn't find it' "
            "card will be posted below your message. Say you couldn't find it and suggest "
            "naming the song/artist."
        )

    if isinstance(result, discord.Embed):
        async def _send_embed():
            # 🌸 mention_author=True: the embed is now the ONLY reply (the model's
            # text is skipped, see card_delivered), so it carries the ping.
            await message.reply(embed=result, mention_author=True, allowed_mentions=_SAFE_MENTIONS)

        ctx.pending.append(_send_embed)
        ctx.delivered = True
        _mark_card_delivered(message.id)
        return "The image will be posted directly BELOW your message as an embed. Do not paste links."

    if isinstance(result, discord.ui.LayoutView):
        async def _send_view():
            await message.reply(view=result, mention_author=True, allowed_mentions=_SAFE_MENTIONS)

        ctx.pending.append(_send_view)
        ctx.delivered = True
        _mark_card_delivered(message.id)
        return "A result card will be posted directly BELOW your message."

    if isinstance(result, str):
        async def _send_text():
            await message.reply(result[:2000], mention_author=False, allowed_mentions=_SAFE_MENTIONS)

        ctx.pending.append(_send_text)
        ctx.delivered = True
        return "The video will be posted directly BELOW your message as a link. Do not paste it again."

    return "The result could not be posted in a supported format."


async def _invoke(fn, *args, **kwargs):
    """🌸 Runs a local handler without ever blocking the event loop:
    coroutine functions are awaited directly (calling asyncio.to_thread
    on an `async def` would just hand back an un-awaited coroutine);
    plain sync callables go to a worker thread."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


class _Ctx:
    """Everything an executor needs, bundled so each one stays tiny."""

    __slots__ = ("service", "message", "shared", "guild_id", "delivered", "pending", "private")

    def __init__(self, service, message, shared_ctx):
        self.service = service
        self.message = message
        self.shared = shared_ctx
        self.guild_id = message.guild.id if message.guild else 0
        self.delivered = False  # set by _deliver once something is queued to post
        self.pending = []       # async callables, flushed AFTER the text reply
        self.private = []       # DM payloads sent this round (for the leak guard)


# ─────────────────────────────────────────────────────────────────────
# Executors — one per tool. Each returns the plain-text tool result.
# ─────────────────────────────────────────────────────────────────────

async def _exec_math(ctx: _Ctx, args: dict) -> str:
    from extras.groq_math import handle_math_request

    expression = str(args.get("expression") or "").strip()
    if not expression:
        return "ERROR: no expression was provided."
    # handle_math_request evaluates in a worker thread itself (a huge
    # expression must not freeze the bot) and posts over-long results to
    # the channel directly — see _handle_math_hint.
    out = await handle_math_request(
        ctx.message, ctx.guild_id, ctx.shared, ctx.service.client,
        expression_hint=expression,
    )
    return out or "ERROR: the calculator returned nothing."


_VALID_KINDS = frozenset({"photo", "video", "vector", "cartoon"})
# 🌸 What models actually write when they don't pick one of the four exactly.
_KIND_ALIASES = {
    "anime": "cartoon", "manga": "cartoon", "illustration": "cartoon",
    "drawing": "cartoon", "art": "cartoon", "animation": "cartoon", "comic": "cartoon",
    "clip": "video", "gif": "video", "movie": "video",
    "image": "photo", "picture": "photo", "pic": "photo", "photograph": "photo",
    "svg": "vector", "vector graphic": "vector", "icon": "vector",
}


#   images) — gets a 50/50 coin flip…" comment down to the end of
#   _exec_media() with this whole block ─────────────────────────────────

# 🌸 Anime/cartoon dice: one random.random() roll per request.
#   0.00 – 0.50 (50%) → nekos.best  (official async `nekosbest` library)
#   0.50 – 0.80 (30%) → nekos.life  (REST)
#   0.80 – 1.00 (20%) → straight to the normal Pixabay path
# Any provider failure (timeout / network / None) also lands on Pixabay.
# Endpoints, fetchers, embed and category detection live in groq_pexels.py;
# only the decision logic stays here. Image-only APIs, so video/vector
# never qualify (see _wants_nekos).
_NEKOS_BEST_UPPER = 0.50
_NEKOS_LIFE_UPPER = 0.80

# Words that make a query count as "anime-flavored" even when the model
# picked kind="photo".
_NEKOS_TRIGGER_RE = re.compile(
    r"\b(anime|manga|neko|nekos|waifu|husbando|chibi|catgirl|cat\s*girl)\b",
    re.IGNORECASE,
)


def _wants_nekos(kind: str, query: str, message_text: str) -> bool:
    """True when this request is eligible for the nekos dice roll:
    kind == "cartoon" (which already includes the anime/manga aliases), or
    an anime/neko/waifu keyword in the query or the user's message. Never
    for video/vector — the nekos APIs only serve still images/GIFs."""
    if kind in ("video", "vector"):
        return False
    if kind == "cartoon":
        return True
    return bool(_NEKOS_TRIGGER_RE.search(f"{query} {message_text}"))


def _roll_nekos_provider() -> tuple[str | None, float]:
    """One roll → ("best" | "life" | None, roll). None means Pixabay."""
    roll = random.random()
    if roll < _NEKOS_BEST_UPPER:
        return "best", roll
    if roll < _NEKOS_LIFE_UPPER:
        return "life", roll
    return None, roll


async def _exec_media(ctx: _Ctx, args: dict) -> str:
    from groq_pexels import handle_media_request, fetch_nekos_embed, pick_nekos_category

    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: no search query was provided."
    kind = str(args.get("kind") or "photo").strip().lower()
    kind = _KIND_ALIASES.get(kind, kind)
    if kind not in _VALID_KINDS:
        kind = "photo"

    # 🌸 Anime-flavored request → roll the 50/30/20 dice.
    message_text = getattr(ctx.message, "content", "") or ""
    if _wants_nekos(kind, query, message_text):
        provider, roll = _roll_nekos_provider()
        if provider is None:
            print(f"🎲 Anime roll {roll:.2f} → Pixabay (20% slice)")
        else:
            category = pick_nekos_category(query, message_text)
            print(f"🎲 Anime roll {roll:.2f} → nekos.{provider} (category={category})")
            embed = None
            try:
                embed = await fetch_nekos_embed(provider, category)
            except Exception as e:  # belt-and-suspenders: the fetcher already never raises
                print(f"⚠️ nekos.{provider} handler crashed ({type(e).__name__}): {e}")
            if embed is not None:
                return await _deliver(ctx, embed)
            print(f"↩️ nekos.{provider} failed, falling back to Pixabay")

    # Pixabay / Pexels path — the 20% slice AND the fallback for every
    # nekos failure above land here.
    result = await handle_media_request(
        ctx.message, ctx.guild_id, ctx.shared,
        query_hint=query, kind_hint=kind,
    )
    if result is None:
        return (
            f"Nothing found for '{query}' (or the media API is unavailable). Tell the "
            "user you couldn't find one and offer to try a different search."
        )
    return await _deliver(ctx, result)


async def _exec_music(ctx: _Ctx, args: dict) -> str:
    from groq_music_suggestion import handle_music_request

    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: no song or artist was provided."
    result = await handle_music_request(ctx.message, ctx.guild_id, ctx.shared, query_hint=query)
    if result is None:
        return "Music search is unavailable right now. Tell the user sweetly and offer to try again."
    return await _deliver(ctx, result)


async def _text_result(ctx: _Ctx, result, *, empty_msg: str, limit: int | None = None) -> str:
    """🌸 Shared tail for the two server lookups: str/Embed → text for the
    model; any other shape (LayoutView, tuple) is delivered to the channel
    instead since it can't be flattened."""
    if result is None:
        return empty_msg
    if isinstance(result, str):
        return _clip(_with_mention_note(result), limit)
    if isinstance(result, discord.Embed):
        return _clip(_with_mention_note(_embed_to_text(result)), limit)
    return await _deliver(ctx, result)


# 🌸 Remembers, per user message, that check_member_existence ran and what it
# returned. groq_service.py uses this as a safety net: if the model skipped
# the tool (and guessed), or called it but replied with only an emoji, the
# reply is rebuilt from the real lookup instead. Bounded so it can't grow.
_MEMBER_RESULTS: "OrderedDict[int, str]" = OrderedDict()
_MEMBER_RESULTS_MAX = 100


def _remember_member_result(message_id: int, text: str) -> None:
    _MEMBER_RESULTS[message_id] = text or ""
    _MEMBER_RESULTS.move_to_end(message_id)
    while len(_MEMBER_RESULTS) > _MEMBER_RESULTS_MAX:
        _MEMBER_RESULTS.popitem(last=False)


def member_tool_ran(message_id: int) -> bool:
    """True if check_member_existence was executed for this user message."""
    return message_id in _MEMBER_RESULTS


def get_member_tool_result(message_id: int):
    """The raw lookup answer (e.g. '✅ Yep, <@id> is here…') or None."""
    return _MEMBER_RESULTS.get(message_id) or None


# 🌸 Message ids whose image embed / music card was queued by a tool.
# groq_service.py skips the model's own text reply for these: the card already
# carries its own caption/summary, and sending both produced two messages.
_CARD_DELIVERED: "OrderedDict[int, bool]" = OrderedDict()


def _mark_card_delivered(message_id: int) -> None:
    _CARD_DELIVERED[message_id] = True
    _CARD_DELIVERED.move_to_end(message_id)
    while len(_CARD_DELIVERED) > _MEMBER_RESULTS_MAX:
        _CARD_DELIVERED.popitem(last=False)


def card_delivered(message_id: int) -> bool:
    """True if a tool queued an image embed or music card for this user message."""
    return message_id in _CARD_DELIVERED


_PROFILE_RULES = (
    "The user explicitly asked about this member's profile picture/banner/looks. "
    "Answer exactly that, in your own voice, using ONLY the PROFILE above — never invent "
    "anything beyond it. Describing the images is fine, but don't turn them into claims "
    "about the person's personality or character. Be warm and kind; no insults, no guesses "
    "about who they really are. If an image couldn't be viewed or isn't set, say that "
    "honestly. If the profile has NITRO_FLAG / DECO_FLAG lines, you may react to them "
    "playfully in character (e.g. treat them like a fancy Nitro member, 'ooh Nitro vibes' / "
    "'that a_ animated pfp is so fancy'); a NITRO_FLAG is a strong hint, a DECO_FLAG alone is "
    "not proof of Nitro, and never state that they are rich or comment on their money."
)

_PROPS_RULES = (
    "The user wants something about this member (a fact, joke, light roast, compliment, "
    "trivia, rating, comparison or just info). Build it ONLY from the PROPS above — never "
    "invent anything beyond it, and never claim to know their personality, hobbies, messages "
    "or private life. Playful teasing about the stats (join date, account age, roles, badges, "
    "boosting, 'ancient account', 'collects roles like trophies') is welcome, but keep it warm "
    "and kind: no real insults, and no jokes about appearance, identity, religion, health, "
    "family or money. Use the exact numbers/dates given. 1-3 short sentences unless they "
    "ask for more, in your usual voice. If they ask for something the PROPS don't cover, say "
    "sweetly that you can't see that."
)


def _member_result_for_model(result: str, profile_block: str | None = None,
                             props_block: str | None = None) -> str:
    """🌸 Drops the leading ✅/❌ (models were copying ONLY that emoji as their
    whole reply) and tells the model to write a real sentence. With a
    profile_block (explicit pfp/banner questions only) it gives an in-character
    take on the member's profile; with a props_block (intent='props') it builds
    a fact/joke/roast/etc. from the member's real server properties; with both,
    both rule sets apply. Neither → a short existence answer."""
    body = re.sub(r"^[\s✅❌]+", "", result).strip()
    if profile_block or props_block:
        parts = [f"LOOKUP RESULT: {body}"]
        rules = []
        if profile_block:
            parts.append(
                "PROFILE (you can't see images — this is a description of what is there):\n"
                f"{profile_block}"
            )
            rules.append(_PROFILE_RULES)
        if props_block:
            parts.append(f"PROPS (real data about this member; nothing else is known):\n{props_block}")
            rules.append(_PROPS_RULES)
        parts.append("(" + " ".join(rules) + " Keep the <@id> mention exactly as written.)")
        return "\n".join(parts)
    return (
        f"LOOKUP RESULT: {body}\n"
        "(Reply with ONE short sentence in your own voice that states this result in "
        "full: who, and whether they are in the server. Never reply with only an emoji. "
        "This lookup tells you NOTHING about their personality or looks — if the user also "
        "asked what they're like / for an opinion, say sweetly you can't really tell from "
        "this, and do NOT describe or mention their profile picture or banner.)"
    )


async def _exec_role(ctx: _Ctx, args: dict) -> str:
    if ctx.message.guild is None:
        return "This only works inside a server, not in DMs."
    from groq_instruct import handle_role_query

    role = str(args.get("role_name") or "").strip().lstrip("@")
    if not role:
        return "ERROR: no role name was provided."
    # 🌸 role_name_hint skips the handler's regex extraction. groq_client
    # is deliberately None: its only use is maybe_rephrase_db_result (a
    # 35% extra Groq call to reword the answer) — redundant now that the
    # main model phrases the final reply from this result itself.
    result = await _invoke(
        handle_role_query, ctx.message, ctx.guild_id, ctx.shared,
        groq_client=None, role_name_hint=role,
    )
    return await _text_result(
        ctx, result,
        empty_msg=f"The role lookup for '{role}' failed. Tell the user you couldn't check right now.",
    )


# ─────────────────────────────────────────────────────────────────────
# Member profile (pfp + banner) for check_member_existence(explicit pfp/banner questions only)
# ─────────────────────────────────────────────────────────────────────

_ID_OR_MENTION_RE = re.compile(r"^\s*<?@?!?(\d{17,20})>?\s*$")
_PROFILE_INTENTS = ("profile", "pfp", "avatar", "banner", "appearance", "look", "image", "picture")

# 🌸 Hard gate on top of the model's intent: the pfp/banner is described ONLY when
# the user's own message explicitly names it or asks about looks. The model alone
# was too eager — it read the pfp for "is he friendly?" and judged personality from it.
_EXPLICIT_PROFILE_RE = re.compile(
    r"\b(pfp|pp|dp|avatar|banner|accent|deco(?:ration)?s?|profile\s*(?:colou?r|pic|picture|photo|image|img)s?|"
    r"foto\s*profil|gambar\s*profil|look(?:s|ing)?(?:\s*like)?|tampang|penampilan)\b",
    re.IGNORECASE,
)


def _wants_profile(args: dict, message_text: str = "") -> bool:
    """True only when the model asked for the profile AND the user's own
    message explicitly mentions the pfp/banner/looks. Lenient on the model's
    wording ('profile', 'pfp_check'…), strict on the user's."""
    intent = str(args.get("intent") or "").strip().lower()
    if not intent.startswith(_PROFILE_INTENTS):
        return False
    return bool(_EXPLICIT_PROFILE_RE.search(message_text or ""))


# 🌸 "my pfp / my banner / aku…": the asker is talking about THEMSELVES. The model sometimes
# grabs the @mention that merely addresses the bot ("@Bruh is my pfp cool") as the target and
# ends up describing the bot's own pfp.
_FIRST_PERSON_RE = re.compile(
    r"\b(?:my|mine|myself|gue|gw|gua|aku|saya)\b|\b(?:pfp|pp|dp|avatar|banner|foto|profil|profile)ku\b",
    re.IGNORECASE,
)


def _is_bot_self(ctx, target: str) -> bool:
    """True when `target` (name, display name, @mention or ID) is the bot itself."""
    guild = ctx.message.guild
    me = guild.me if guild else None
    if me is None:
        return False
    id_match = _ID_OR_MENTION_RE.match(target or "")
    if id_match:
        return int(id_match.group(1)) == me.id
    t = (target or "").strip().lstrip("@").lower()
    names = {
        n.lower()
        for n in (getattr(me, "display_name", None), getattr(me, "name", None), getattr(me, "global_name", None))
        if n
    }
    return bool(t) and t in names


_SELF_WORDS = frozenset({"me", "myself", "aku", "gue", "gw", "gua", "saya"})


def _resolve_asker_target(ctx, target: str) -> str:
    """🌸 The bot's own name/mention is never the lookup target when the asker says
    'my pfp/banner/…': use the asker's ID instead (an ID resolves exactly). A bare
    'me' / 'myself' / 'aku' target ('roast me') is the asker too."""
    if (target or "").strip().lstrip("@").lower() in _SELF_WORDS:
        return str(ctx.message.author.id)
    if _is_bot_self(ctx, target) and _FIRST_PERSON_RE.search(getattr(ctx.message, "content", "") or ""):
        return str(ctx.message.author.id)
    return target


# 🌸 Plain "profile" counts here (the stricter _EXPLICIT_PROFILE_RE wants "profile pic…"),
# because it only fires together with a first-person word AND a bot-self/asker target.
_SELF_PROFILE_WORDS_RE = re.compile(
    r"\b(pfp|pp|dp|avatar|banner|accent|deco(?:ration)?s?|profile|profil|"
    r"foto\s*profil|gambar\s*profil|look(?:s|ing)?|tampang|penampilan)\b",
    re.IGNORECASE,
)


def _is_asker(ctx, target: str) -> bool:
    """True when `target` (name / display name / @mention / ID) is the person who sent the message."""
    author = ctx.message.author
    id_match = _ID_OR_MENTION_RE.match(target or "")
    if id_match:
        return int(id_match.group(1)) == author.id
    t = (target or "").strip().lstrip("@").lower()
    names = {
        n.lower()
        for n in (getattr(author, "display_name", None), getattr(author, "name", None), getattr(author, "global_name", None))
        if n
    }
    return bool(t) and t in names


def _is_self_profile_question(ctx, target: str) -> bool:
    """🌸 'is my pfp/profile cool' — the asker wants THEIR OWN profile looked at. True when the
    message is first-person + profile words and the model's target is the bot itself (the
    @mention that addresses the bot) or the asker. Independent of the model's `intent` and of
    the unseen name lookup, which kept resolving the '@Bruh' text."""
    text = getattr(ctx.message, "content", "") or ""
    if not (_FIRST_PERSON_RE.search(text) and _SELF_PROFILE_WORDS_RE.search(text)):
        return False
    return _is_bot_self(ctx, target) or _is_asker(ctx, target)


def _is_default_avatar(url) -> bool:
    return bool(url) and "/embed/avatars/" in url


def _is_animated_hash(url) -> bool:
    """🌸 True when a Discord CDN link's hash starts with a_ (animated).
    Looks at the last path segment only (…/avatars/<id>/a_<hash>.png,
    …/avatar-decoration-presets/a_<hash>.png) — never LIKE '%a_%' in SQL,
    where _ is a wildcard and everything matches."""
    if not url:
        return False
    last = str(url).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return last.startswith("a_")


def _still_image_url(url: str, size: int = 512) -> str:
    """🌸 Vision-friendly CDN link: animated hashes (a_…) get a .png (first
    frame — Discord serves that), and the size is capped so the image
    download stays small."""
    base = url.split("?", 1)[0]
    if base.endswith((".gif", ".webp")):
        base = base.rsplit(".", 1)[0] + ".png"
    return f"{base}?size={size}"


async def _find_target_profile(ctx: _Ctx, target: str, user_id: int | None = None):
    """🌸 The members.db profile dict (names + avatar_url + banner_url) of the
    ONE member `target` resolves to, or None (0 or 2+ matches, db not
    ready). Same priority as handle_member_lookup_query: a real non-bot
    @mention beats the model's name text."""
    shared = ctx.shared
    guild = ctx.message.guild
    try:
        if not await shared.members_db_ready(ctx.guild_id):
            return None
        me_id = guild.me.id if guild and guild.me else None
        real = [m for m in ctx.message.mentions if m.id != me_id]
        if user_id:  # 🌸 forced (self-profile question): exact ID, mentions/names ignored
            matches = await shared.find_member_profile_by_id(ctx.guild_id, user_id)
        elif real:
            matches = await shared.find_member_profile_by_id(ctx.guild_id, real[0].id)
        else:
            id_match = _ID_OR_MENTION_RE.match(target)
            if id_match:
                matches = await shared.find_member_profile_by_id(ctx.guild_id, int(id_match.group(1)))
            else:
                matches = await shared.find_member_profile_by_name(ctx.guild_id, target)
    except Exception as e:
        print(f"⚠️ Member profile lookup failed ({type(e).__name__}): {e}")
        return None
    return matches[0] if len(matches) == 1 else None


async def _describe_profile_images(ctx: _Ctx, avatar_url, banner_url, decoration_url=None) -> dict:
    """🌸 ONE vision call for whichever of pfp/banner/avatar-decoration exist →
    {"avatar": str, "banner": str, "decoration": str} (a key is missing when
    that image wasn't described).
    Never raises; {} on any failure."""
    items = [
        (label, url)
        for label, url in (("avatar", avatar_url), ("banner", banner_url), ("decoration", decoration_url))
        if url
    ]
    if not items:
        return {}
    key = (avatar_url, banner_url, decoration_url)
    cached = _PROFILE_DESC_CACHE.get(key)
    if cached is not None:
        _PROFILE_DESC_CACHE.move_to_end(key)
        return cached

    client = getattr(ctx.service, "client", None)
    if client is None:
        return {}

    labels = ", ".join(label.upper() for label, _ in items)
    prompt = (
        f"These are a Discord user's {labels} image(s), in that order"
        + (" (DECORATION is a decorative frame/effect drawn around the avatar, e.g. a border, "
           "flames or sparkles)" if decoration_url else "")
        + ". For each one write "
        "ONE short line (max 25 words): the subject, art style, main colors and mood, plus "
        "any readable text. Describe only what is visibly there — do NOT identify real "
        "people or guess age, ethnicity or anything about who they are. Format exactly:\n"
        + "\n".join(f"{label.upper()}: <description>" for label, _ in items)
    )
    content = [{"type": "text", "text": prompt}]
    for _, url in items:
        content.append({"type": "image_url", "image_url": {"url": _still_image_url(url)}})

    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat.completions.create,
                model=PROFILE_VISION_MODEL,
                messages=[{"role": "user", "content": content}],
                max_tokens=_PROFILE_VISION_MAX_TOKENS,
                temperature=0.2,
            ),
            timeout=_PROFILE_VISION_TIMEOUT,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"⚠️ Profile image vision failed ({type(e).__name__}): {e}")
        return {}
    # 🌸 qwen reasoning output: drop a <think>…</think> block if the API left it inline.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    if not text:
        print(f"⚠️ Profile image vision returned empty content ({PROFILE_VISION_MODEL}) — "
              "reasoning probably ate max_tokens; raise _PROFILE_VISION_MAX_TOKENS")
        return {}

    out = {}
    for label, _ in items:
        m = re.search(rf"{label.upper()}\s*:\s*(.+)", text, re.IGNORECASE)
        if m:
            out[label] = m.group(1).strip()
    if not out and len(items) == 1 and text:
        out[items[0][0]] = text  # model skipped the "AVATAR:" prefix
    if out:
        _PROFILE_DESC_CACHE[key] = out
        _PROFILE_DESC_CACHE.move_to_end(key)
        while len(_PROFILE_DESC_CACHE) > _PROFILE_DESC_CACHE_MAX:
            _PROFILE_DESC_CACHE.popitem(last=False)
    return out


async def _profile_block(ctx: _Ctx, target: str, user_id: int | None = None):
    """🌸 The 'how they look' text appended to a successful member lookup, or
    None when the member can't be resolved to exactly one profile."""
    profile = await _find_target_profile(ctx, target, user_id)
    if not profile:
        return None

    avatar = profile.get("avatar_url")
    banner = profile.get("banner_url")
    deco = profile.get("avatar_decoration_url")
    accent = profile.get("accent_color")
    has_avatar = bool(avatar) and not _is_default_avatar(avatar)
    has_banner = bool(banner)
    has_deco = bool(deco)
    descs = await _describe_profile_images(
        ctx,
        avatar if has_avatar else None,
        banner if has_banner else None,
        deco if has_deco else None,
    )

    unviewable = "exists but couldn't be viewed right now — don't guess what it shows"
    lines = []
    names = [n for n in (profile.get("global_name"), profile.get("username"), profile.get("server_name")) if n]
    if names:
        lines.append("Names: " + " / ".join(dict.fromkeys(names)))
    lines.append(
        "Profile picture: " + (descs.get("avatar") or unviewable) if has_avatar
        else "Profile picture: none set (default Discord avatar)"
    )
    lines.append(
        "Banner: " + (descs.get("banner") or unviewable) if has_banner
        else "Banner: none set"
    )
    lines.append(
        "Avatar decoration: " + (descs.get("decoration") or unviewable) if has_deco
        else "Avatar decoration: none"
    )
    lines.append(f"Accent colour: {accent}" if accent else "Accent colour: none set")

    # 🌸 AI flags: cosmetics that hint at Nitro, so the model can treat the
    # member like a fancy Nitro user. Hints only — never proof of wealth.
    flags = []
    if accent:
        flags.append(
            "NITRO_FLAG accent_colour: a custom profile accent colour is a Nitro feature, "
            "so this member most likely has Nitro (a fancy Nitro member)."
        )
    if has_avatar and _is_animated_hash(avatar):
        flags.append(
            "NITRO_FLAG animated_pfp: their profile picture hash starts with a_ (animated GIF pfp) — "
            "animated avatars need Nitro, so they definitely have/had Nitro."
        )
    if has_deco and _is_animated_hash(deco):
        flags.append(
            "DECO_FLAG animated_decoration: their avatar decoration hash starts with a_ "
            "(an animated frame). Decorations come from the Discord Shop (cheaper/free with Nitro), "
            "so this is a premium-cosmetic flex but NOT proof of Nitro by itself."
        )
    elif has_deco:
        flags.append(
            "DECO_FLAG static_decoration: they wear a (non-animated) avatar decoration from the "
            "Discord Shop — a premium cosmetic, but NOT proof of Nitro by itself."
        )
    if flags:
        lines.append("Flags:")
        lines.extend(f"- {f}" for f in flags)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Member props (intent='props') — facts / jokes / light roasts / trivia
# ─────────────────────────────────────────────────────────────────────

_PROPS_INTENTS = frozenset({
    "props", "prop", "properties", "info", "facts", "fact", "fun", "joke", "jokes",
    "roast", "compliment", "trivia", "stats", "details", "about",
})

# Badges that say nothing fun about a person (or shouldn't be mentioned).
_HIDDEN_BADGES = frozenset({"spammer", "team_user", "system", "quarantined"})


def _wants_props(args: dict) -> bool:
    """True when the model asked for the member's properties. Unlike
    _wants_profile there is NO regex gate on the user's text: props are plain
    public server data, and the whole point is 'anything about this member'."""
    intent = str(args.get("intent") or "").strip().lower()
    return bool({t for t in re.split(r"[^a-z]+", intent) if t} & _PROPS_INTENTS)


def _member_id_from_result(result: str) -> int | None:
    """The member's ID out of a clean single-hit lookup ('✅ Yep, <@id> is here…')."""
    m = re.search(r"<@!?(\d{17,20})>", result or "")
    return int(m.group(1)) if m else None


def _humanize_age(dt) -> str:
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = max((datetime.now(timezone.utc) - dt).days, 0)
    years, rem = divmod(days, 365)
    months = rem // 30
    if years:
        out = f"{years} year{'s' if years != 1 else ''}"
        return out + (f" {months} month{'s' if months != 1 else ''}" if months else "")
    if months:
        return f"{months} month{'s' if months != 1 else ''}"
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    return "less than a day"


def _fmt_date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "unknown"


async def _fetch_member_obj(ctx: _Ctx, user_id: int):
    """discord.Member from cache, else one REST fetch. None if they're gone."""
    guild = ctx.message.guild
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None


async def _member_props_block(ctx: _Ctx, user_id: int):
    """🌸 Real, public server properties of ONE member as compact text for the
    model to make facts / jokes / light roasts / trivia from. Never raises;
    None when the member object can't be obtained. Only things Discord itself
    shows on a profile — no message history, nothing private."""
    try:
        m = await _fetch_member_obj(ctx, user_id)
        if m is None:
            return None
        guild = ctx.message.guild
        lines = []

        names = [f"Display name: {m.display_name}", f"Username: {m.name}"]
        gname = getattr(m, "global_name", None)
        if gname and gname != m.display_name:
            names.append(f"Global name: {gname}")
        if getattr(m, "nick", None):
            names.append(f"Server nickname: {m.nick}")
        lines.append(" | ".join(names))
        if m.bot:
            lines.append("Type: this account is a BOT")

        created = getattr(m, "created_at", None)
        lines.append(f"Discord account created: {_fmt_date(created)} ({_humanize_age(created)} old)")
        joined = getattr(m, "joined_at", None)
        lines.append(f"Joined this server: {_fmt_date(joined)} ({_humanize_age(joined)} ago)")

        # Join order — only when the member cache is (nearly) complete, else it lies.
        total = getattr(guild, "member_count", 0) or 0
        cached = len(guild.members)
        if joined and total and 0.9 * total <= cached <= 50000:
            earlier = sum(1 for x in guild.members if x.joined_at and x.joined_at < joined)
            lines.append(f"Join order: #{earlier + 1} of {total} members (#1 = earliest, approximate)")

        roles = [r for r in reversed(m.roles) if not r.is_default()]
        if roles:
            shown = ", ".join(r.name for r in roles[:_PROPS_MAX_ROLES])
            more = f" (+{len(roles) - _PROPS_MAX_ROLES} more)" if len(roles) > _PROPS_MAX_ROLES else ""
            lines.append(f"Roles ({len(roles)}, highest first): {shown}{more}")
        else:
            lines.append("Roles: none")
        if getattr(m.color, "value", 0):
            lines.append(f"Name colour: #{m.color.value:06x}")

        premium = getattr(m, "premium_since", None)
        lines.append(f"Server booster since: {_fmt_date(premium)}" if premium else "Server booster: no")

        if guild.owner_id == m.id:
            lines.append("Is the server OWNER")
        elif m.guild_permissions.administrator:
            lines.append("Has Administrator permission")
        try:
            if m.is_timed_out():
                lines.append("Currently timed out")
        except Exception:
            pass

        try:
            badges = [
                f.name.replace("_", " ") for f in m.public_flags.all()
                if f.name not in _HIDDEN_BADGES
            ]
        except Exception:
            badges = []
        if badges:
            lines.append("Badges: " + ", ".join(badges))

        # Presence only when Discord actually reports it (needs the presence
        # intent; without it everyone is 'offline', which would be a lie).
        status = str(getattr(m, "status", "offline"))
        if status != "offline":
            lines.append(f"Status right now: {status}")
        acts = []
        for a in (getattr(m, "activities", None) or ())[:3]:
            if isinstance(a, discord.CustomActivity):
                if a.name:
                    acts.append(f'custom status "{str(a.name)[:60]}"')
            elif getattr(a, "name", None):
                kind = getattr(getattr(a, "type", None), "name", "activity")
                acts.append(f"{kind} {str(a.name)[:60]}")
        if acts:
            lines.append("Activity: " + "; ".join(acts))

        return "\n".join(lines)[:_PROPS_MAX_CHARS]
    except Exception as e:
        print(f"⚠️ Member props failed ({type(e).__name__}): {e}")
        return None


async def _exec_self_profile(ctx: _Ctx, want_props: bool = False) -> str:
    """🌸 The asker's OWN profile ('is my pfp cool'): no name lookup at all — the author's ID
    goes straight to members.db, so '@Bruh' (the bot) can never be picked up as the target.
    want_props also adds their server properties ('roast my pfp and join date')."""
    author = ctx.message.author
    profile_block = await _profile_block(ctx, str(author.id), user_id=author.id)
    props_block = await _member_props_block(ctx, author.id) if want_props else None
    result = (
        f"✅ Yep, <@{author.id}> is here — that's the person asking, and they asked about THEIR OWN "
        "profile (say 'your', not 'their')."
    )
    _remember_member_result(ctx.message.id, result)
    out = _member_result_for_model(result, profile_block, props_block)
    if not profile_block:
        out += (
            "\n(Their profile couldn't be loaded right now — tell them sweetly you can't see it, "
            "and don't guess what it looks like.)"
        )
    return _clip(out, _MEMBER_RESULT_MAX_CHARS)


async def _exec_member(ctx: _Ctx, args: dict) -> str:
    if ctx.message.guild is None:
        return "This only works inside a server, not in DMs."
    from groq_instruct import handle_member_lookup_query

    target = str(args.get("target_name") or "").strip().lstrip("@")
    if not target:
        return "ERROR: no member name was provided."
    if _is_self_profile_question(ctx, target):
        return await _exec_self_profile(ctx, _wants_props(args))
    target = _resolve_asker_target(ctx, target)
    # 🌸 name_hint takes priority over the handler's regex fallback (a
    # real non-bot @mention in the message still wins, as before).
    # groq_client=None skips the optional rephrase call — see _exec_role.
    result = await _invoke(
        handle_member_lookup_query, ctx.message, ctx.guild_id, ctx.shared,
        groq_client=None, name_hint=target,
    )
    if isinstance(result, str):
        _remember_member_result(ctx.message.id, result)
        # 🌸 Explicit pfp/banner/looks question about ONE found member → also hand
        # the model a description of their pfp + banner. Only a clean single
        # hit ("✅ Yep, <@id> is here…") qualifies; not-found / ambiguous
        # results keep the plain existence answer.
        profile_block = None
        props_block = None
        found = result.lstrip().startswith("✅")
        if found and _wants_profile(args, getattr(ctx.message, "content", "") or ""):
            profile_block = await _profile_block(ctx, target)
        # 🌸 intent='props' → real server properties for facts/jokes/roasts/trivia.
        # The member id comes from the lookup's own '<@id>' hit, so it can only
        # ever describe the ONE member the handler actually found.
        if found and _wants_props(args):
            member_id = _member_id_from_result(result)
            if member_id:
                props_block = await _member_props_block(ctx, member_id)
        result = _member_result_for_model(result, profile_block, props_block)
    else:
        _remember_member_result(ctx.message.id, "")
    return await _text_result(
        ctx, result,
        empty_msg=(
            f"Couldn't complete the member lookup for '{target}' (unusable name or a lookup "
            "error). Tell the user you couldn't check and ask for the exact username."
        ),
        limit=_MEMBER_RESULT_MAX_CHARS,
    )


async def _exec_dm(ctx: _Ctx, args: dict) -> str:
    if ctx.message.guild is None:
        return "This is already a DM, so there's nothing to send privately — just reply normally."
    from extras.groq_dm_instruct import send_private_dm

    content = str(args.get("content") or "").strip()
    ok, result = await send_private_dm(ctx.message, content)
    if ok:
        # 🌸 Remember what was sent so completion.py can scrub it if the
        # model repeats it in the public reply (redact_dm_leaks).
        ctx.private.append(content)
    return result


async def _exec_snowflake(ctx: _Ctx, args: dict) -> str:
    from extras.groq_dm_instruct import describe_snowflake

    return describe_snowflake(str(args.get("snowflake") or ""))


async def _exec_search(ctx: _Ctx, args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: no search query was provided."
    exa = getattr(ctx.service, "exa", None)
    if exa is None:
        return "Web search is not available right now. Tell the user sweetly and answer only what you're sure of."
    # 🌸 ExaSearchService.search = answer() → search_summary() → Currents+Jina,
    # each tier already hard-capped, so the result is a SHORT string (the
    # whole reason Exa replaced compound's raw-snippet dump — see its
    # class docstring). category_hint skips its own classifier AI call.
    result = await exa.search(query, category_hint=str(args.get("category") or "") or None)
    if not result:
        return (
            f"The web search for '{query}' found nothing usable. Say you couldn't find "
            "reliable info and don't guess."
        )
    return (
        "Web search result (summarize it in your own words and in character; if it "
        f"doesn't fully answer the question, say so):\n{result}"
    )


async def _exec_mood(ctx: _Ctx, args: dict) -> str:
    from extras.emotion_detector import react_to_mood_tool

    return await react_to_mood_tool(ctx.message, str(args.get("mood") or ""))


_EXECUTORS = {
    "search_web": _exec_search,
    "react_to_mood": _exec_mood,
    "calculate_local_math": _exec_math,
    "search_visual_media": _exec_media,
    "search_and_play_music": _exec_music,
    "query_peran_server": _exec_role,
    "check_member_existence": _exec_member,
    "send_dm_to_user": _exec_dm,
    "decode_discord_snowflake": _exec_snowflake,
}


async def run_tool_calls(service, response_message, *, message, shared_ctx) -> tuple[list, bool, list, list]:
    """🌸 Executes every tool call in one model response.

    Returns (tool_messages, delivered, pending, private):
      tool_messages — one {"role": "tool", "tool_call_id": ..., "content": ...}
                      per call, in order, ready to extend into `messages`.
      delivered     — True if any tool queued media/music for the channel,
                      so the caller can switch tools off for the follow-up
                      and avoid a duplicate post.
      pending       — the queued async sends (see _deliver). NOT run here:
                      they must go out only after the model's text reply
                      has been sent, so the card appears below it.
      private       — texts sent to the user's DMs this round; completion.py
                      scrubs them from the public reply if the model echoes
                      them (extras.groq_dm_instruct.redact_dm_leaks).

    Never raises: a bad JSON argument, an unknown tool, a handler
    exception or a timeout all become an error string in that call's
    tool message, so the model can apologise in words instead of the
    whole reply dying (and Groq never sees an unanswered tool_call_id).
    """
    ctx = _Ctx(service, message, shared_ctx)
    tool_messages = []

    for tc in response_message.tool_calls:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (ValueError, TypeError) as e:
            print(f"⚠️ Tool '{name}': bad JSON arguments ({e}): {tc.function.arguments!r}")
            content = f"ERROR: the arguments for {name} were not valid JSON."
        else:
            executor = _EXECUTORS.get(name)
            if executor is None:
                print(f"⚠️ Model called unknown tool {name!r}")
                content = f"ERROR: unknown tool '{name}'."
            else:
                print(f"🛠️ Tool call: {name}({args})")
                try:
                    content = await asyncio.wait_for(executor(ctx, args), timeout=TOOL_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    print(f"⚠️ Tool '{name}' timed out after {TOOL_TIMEOUT_SECONDS}s")
                    content = f"ERROR: {name} took too long and was cancelled."
                except TypeError as e:
                    # Most likely cause: a handler doesn't accept the *_hint
                    # kwarg yet. Log loudly — silently falling back to the old
                    # regex-on-message.content path would hide the bug.
                    print(f"⚠️ Tool '{name}' handler rejected its arguments (missing *_hint param?): {e}")
                    content = f"ERROR: {name} is not available right now."
                except Exception as e:
                    print(f"⚠️ Tool '{name}' failed ({type(e).__name__}): {e}")
                    content = f"ERROR: {name} failed."

        tool_messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "name": name,
            "content": _clip(content, _MEMBER_RESULT_MAX_CHARS if name == "check_member_existence" else None),
        })

    return tool_messages, ctx.delivered, ctx.pending, ctx.private
