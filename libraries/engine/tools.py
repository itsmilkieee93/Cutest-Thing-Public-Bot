"""🌸 engine/tools.py, Groq custom Tools / Function Calling for the main
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
    pick_random_member        -> shared.get_random_members (SQLite ORDER BY RANDOM())
                                 (+ intent='props': fact/joke from the pick's real member props;
                                  opinion questions ('favorite/best member') also get real stats:
                                  shared.get_top_senior_member + get_top_role_collector +
                                  get_top_profile_style_member; for opinion questions the style
                                  winner's avatar/banner is read by PROFILE_VISION_MODEL)
    pick_random_role          -> shared.get_random_roles   (SQLite ORDER BY RANDOM())

Division of labor (so completion.py stays small):
  - TOOL_SCHEMAS / get_tool_schemas()  → what _call() puts in kwargs["tools"]
  - assistant_tool_call_message()      → turns Groq's response message into
                                         the dict that goes back into `messages`
  - run_tool_calls()                   → executes every tool call in one model
                                         response and returns the role="tool"
                                         messages (one per tool_call_id, Groq
                                         400s if any call is left unanswered,
                                         so this function NEVER raises).

Handler imports are lazy (inside each executor) because groq_service.py
imports engine.* at module load, importing it back at the top of this
file would be circular.
"""
import colorsys
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
GUILD_ONLY_TOOLS = frozenset({
    "query_peran_server", "check_member_existence", "send_dm_to_user",
    "pick_random_member", "pick_random_role",
})

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


def _tool(name: str, description: str, properties: dict, required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


# 🌸 Wording is deliberately terse (these schemas ride along on EVERY tool call = tokens).
# Several args are plain strings/ints on purpose, never enums/booleans: Groq validates tool
# arguments server-side and rejects the WHOLE call (400 tool_use_failed) when a strict-typed
# argument is slightly off ("kind"/"intent"/"count"). Allowed values live in the descriptions;
# the executors normalize aliases.
TOOL_SCHEMAS = [
    _tool(
        "search_web",
        "Live web search (Exa) → short summarized answer. Use for anything current or real-world "
        "you can't know reliably: news, scores, prices, weather, releases, 'latest', 'who is the "
        "current…', recent events, shaky facts. NOT for chit-chat, opinions, math, or questions "
        "about this bot/server/members (use the server tools).",
        {
            "query": _str("Clear, self-contained query in English (user's language for local topics), "
                          "with names/context from the chat, it is searched on its own."),
            "category": _str("Optional: news, research paper, company, financial report, github, pdf, "
                             "tweet, personal site, linkedin profile, people. Omit for general questions."),
        },
        ["query"],
    ),
    _tool(
        "react_to_mood",
        "Add a matching emoji reaction + get tone guidance. AT MOST ONCE, only when the mood is "
        "clearly upset/sad, stressed/anxious, angry/frustrated or excited/hyped, never for "
        "neutral, jokey or purely informational messages.",
        {"mood": _str("Exactly one of: upset, stressed, angry, excited.")},
        ["mood"],
    ),
    _tool(
        "calculate_local_math",
        "Exact deterministic calculator. Use whenever a number must be computed (arithmetic, %, "
        "powers, roots, trig, factorials…), never do it yourself. NOT for dates, IDs, or numbers "
        "with nothing to compute.",
        {"expression": _str("Raw Python-syntax expression: * ** sqrt() sin() pi. E.g. '9*8*9*9', "
                            "'sqrt(144)', '15% of 80' -> '0.15*80'. No chatter like 'send it to my dm'.")},
        ["expression"],
    ),
    _tool(
        "search_visual_media",
        "Find and post a real image/video. Use when asked to send/show/find a picture, photo, video, "
        "clip, vector, cartoon, illustration or anime/manga art. Default kind=photo for ordinary "
        "subjects (places, nature, animals, food, objects). NOT for songs/audio "
        "(use search_and_play_music).",
        {
            "query": _str("Short English subject, 1-5 words, no filler ('red panda', 'sunset beach')."),
            "kind": _str("Exactly one of: photo (real photograph), video (real clip), vector (flat "
                         "graphic), cartoon (drawn art, ONLY if the user's CURRENT message literally "
                         "asks for anime, manga, cartoon or illustration; never infer it from earlier "
                         "messages). Unsure → photo."),
        },
        ["query", "kind"],
    ),
    _tool(
        "search_and_play_music",
        "Search YouTube Music, post a playable song card with links. Use for a song, track, artist, "
        "playlist or music recommendation. NOT for images/videos.",
        {"query": _str("Song title and/or artist ('Bohemian Rhapsody Queen'). For vague asks "
                       "('something chill') pick a concrete well-known song/artist.")},
        ["query"],
    ),
    _tool(
        "query_peran_server",
        "Look up a ROLE in this server: holders and details. You have NO role data, MUST call for "
        "'who has the Moderator role', 'members of role X'; never list names from a guess. Server-only.",
        {"role_name": _str("Role name without @, OR its ID / <@&ID> mention exactly as written.")},
        ["role_name"],
    ),
    _tool(
        "pick_random_member",
        "Pick RANDOM member(s) of this server, the database rolls the dice. You have NO member "
        "list: MUST call for 'random member', 'pick someone', 'who should…', 'siapa random', "
        "'pilih member acak'; never choose names yourself. Also call for opinion questions ('your "
        "favorite member?', 'which member do you like?', 'who is the best/cutest/coolest?') with no "
        "intent or 'opinion': the result carries REAL server stats (most senior, biggest role "
        "collector, best avatar/banner/accent-colour profile) to form your own favorite from. "
        "intent='props' when they also want a joke, roast, compliment or trivia about the pick "
        "(you get its REAL properties to build from); intent='funfact' for just a fun fact. Optional role filter. Bots never picked. "
        "Re-call for every new request (fresh roll). Server-only.",
        {
            "count": {"type": "integer", "description": "How many (default 1, max 10)."},
            "intent": _str("'props' ONLY for a joke/roast/compliment/trivia/'tell me about them' about "
                           "the pick. 'funfact' for a fun fact. 'opinion' or omit for "
                           "favorite/best/cutest questions and plain picks."),
            "role_name": _str("Optional: only pick holders of this role, name without @, OR ID / "
                              "<@&ID> exactly as written. Omit for the whole server."),
        },
        [],
    ),
    _tool(
        "pick_random_role",
        "Pick RANDOM role(s) of this server, the database rolls the dice. You have NO role list: "
        "MUST call for 'random role', 'pick a role for me', 'role acak'; never invent names. "
        "@everyone and bot/integration roles are never picked. Server-only.",
        {"count": {"type": "integer", "description": "How many (default 1, max 10)."}},
        [],
    ),
    _tool(
        "send_dm_to_user",
        "DM the user privately. Use when they explicitly ask ('dm me that', 'kirim ke dm dong', "
        "'send it privately') or when what you'd share is genuinely sensitive (secret, redeem code, "
        "password, personal results). Rarely needed. Server-only.",
        {"content": _str("ONLY the private text, in full, it must NOT also appear in your public reply.")},
        ["content"],
    ),
    _tool(
        "decode_discord_snowflake",
        "Decode a Discord ID (user/server/channel/message) into its exact creation date and time. "
        "Use when given an ID and asked when it was created / how old the account, server or message "
        "is. IDs are public, no lookup needed.",
        {"snowflake": _str("17-20 digit ID, digits only (strip any <@ > wrapper).")},
        ["snowflake"],
    ),
    _tool(
        "check_member_existence",
        "Look up a person in this server. You have NO member list: MUST call for ANY 'is X "
        "here/there/in this server' question, however casual or typo'd; never answer yes/no from a "
        "guess, and call AGAIN even if asked before (membership changes; old answers are stale). "
        "Also call for ANYTHING else about a specific member, joke, light roast, compliment, "
        "trivia, rating, comparison, 'tell me about X', 'how long has X been here', 'what roles does "
        "X have', opinion, with intent='props': you get their REAL server properties (account age, "
        "join date, roles, nickname, booster, badges, status) and build ONLY from those; you know "
        "nothing of their personality or private life, so never invent it. A plain 'fun fact about X' "
        "→ intent='funfact' (a random fun fact, no Discord stats; it may tease their name, and "
        "their avatar/banner too when the user names them). intent='profile' ONLY when "
        "the user EXPLICITLY asks about their profile picture/pfp/avatar/banner/decoration/accent "
        "colour or how they look ('profile,props' for both), never for a mere opinion. For the "
        "asker's OWN profile ('is my pfp cool') pass the asker's name; the @mention addressing YOU "
        "(the bot) is never the target. NOT for celebrities/public figures who aren't members. "
        "Server-only.",
        {
            "target_name": _str("Member's name, display name or username (may be several words), OR "
                                "Discord ID (17-20 digits) OR <@ID> mention, copied exactly as the "
                                "user wrote it."),
            "intent": _str("Optional. 'props' for ANYTHING beyond plain yes/no: joke, roast, "
                           "compliment, trivia, rating, comparison, opinion, 'tell me about X'. "
                           "'funfact' for a fun fact (also when they want it to joke about the name/avatar/banner). "
                           "'profile' ONLY when the user explicitly asks about their pfp/avatar/banner/"
                           "decoration/accent colour or looks. 'profile,props' for both. Omit for a "
                           "plain 'is X here'."),
        },
        ["target_name"],
    ),
]


# 🌸 Appended to the system prompt ONLY on calls that actually carry
# tools (see _call in completion.py). Without it models tend to paste the
# media link themselves or restate a whole embed after the tool already
# delivered it.
TOOL_USE_DIRECTIVE = (
    "\n\n[Tools] RULES:\n"
    "1. You have NO data on who is in this server or what roles exist. Whether a person is "
    "in/on/here/there, or who holds a role, MUST go through check_member_existence / "
    "query_peran_server, never answer yes/no or name people from a guess, even if obvious, and "
    "re-call even if you answered before. NEVER reply with a lone emoji: write a short sentence "
    "naming the person. "
    "Anything about a specific member (joke, light roast, compliment, trivia, rating, 'tell "
    "me about X', 'how long has X been here', opinion) → check_member_existence intent='props', "
    "built ONLY from the returned properties; never invent personality, hobbies or private "
    "details, and if they ask for what the properties don't cover, say sweetly you can't see that. "
    "A plain 'fun fact about X' → intent='funfact': a random fun fact, no Discord stats, may "
    "tease their name (and avatar/banner if the user names them). "
    "intent='profile' ('profile,props' for both) ONLY when the user EXPLICITLY asks about "
    "pfp/banner/decoration/accent colour/looks. For the ASKER's own 'my pfp', target_name is the "
    "asker's name, the @mention addressing you is NEVER the target.\n"
    "Random member ('pick someone', 'siapa random', 'random person with role X') → "
    "pick_random_member; random role → pick_random_role. Opinion questions ('your favorite "
    "member?', 'which member do you like?', 'who is the best/cutest/coolest?') → pick_random_member "
    "WITHOUT 'props': its result carries real server stats (elder, role collector, best "
    "avatar/banner/accent profile), announce YOUR pick from them like a personal favorite. A "
    "joke/roast about the pick → intent='props', built ONLY from the PROPS returned; a fun fact → "
    "intent='funfact'. The "
    "database already rolled the dice: announce exactly what it returns, never swap or re-pick "
    "names.\n"
    "2. Math: always calculate_local_math; never compute it yourself or alter a returned number.\n"
    "3. Pictures/videos/anime art → search_visual_media (kind=photo unless the user's CURRENT "
    "message explicitly says anime/manga/cartoon/illustration/vector/video; earlier anime pictures "
    "don't count). Songs/artists → search_and_play_music. The card/image posts automatically right "
    "BELOW your message: write one short, cute, in-character lead-in; don't paste links or repeat "
    "its contents.\n"
    "4. Private/sensitive content or an explicit 'DM me' → send_dm_to_user (never repeat that "
    "content publicly). Discord ID to date → decode_discord_snowflake.\n"
    "5. Normal chat needs no tool.\n"
    "6. Current/real-world info (news, scores, prices, weather, 'latest', releases, who holds a "
    "position) → search_web, never from memory; summarize in your own words.\n"
    "7. Clearly upset, stressed, angry or excited user → react_to_mood once, follow its tone "
    "guidance; skip for neutral/jokey.\n"
    "8. On a tool error or nothing found, say so honestly and sweetly. Never invent links, images, "
    "songs or member lists.\n"
    "9. STYLE: never write a dash as punctuation: no em dash (—), no en dash (–), no ' - ' "
    "between words. Use a comma, period or new sentence instead: write 'great, thanks', never "
    "'great — thanks'. Hyphens inside words and dates (well-known, 2026-07-13) are fine."
)

# 🌸 Appended to the system prompt when a 400 forces a retry WITHOUT tools.
# Without it the model just improvises (it once invented a fake imgur link
# for an image request) — this tells it the honest move.
TOOLS_UNAVAILABLE_NOTE = (
    "\n\n[Note] Tools (media, music, math, server lookups) are unavailable for this reply. If the "
    "message needed one, say sweetly you couldn't just now and they can retry. Never invent links, "
    "images, songs, math results or member/role info. Never write dashes as punctuation (— – or ' - '): use commas or periods."
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
        return text + "\n(Copy every <@id>/<@&id> mention verbatim, it renders as a clickable name.)"
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
   , and returns a short stub string for the model.

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

    The deferred sends use mention_author=False, the text reply already
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


# 🌸 The USER's own words decide "drawn art vs real photo" — never the model's
# guess alone. Chat history full of anime cards makes the model pick
# kind="cartoon" (or write 'anime girl') even for "mountain image".
_ART_REQUEST_RE = re.compile(
    r"\b(anime|manga|neko|nekos|waifu|husbando|chibi|catgirl|cat\s*girl|"
    r"cartoon|kartun|illustration|ilustrasi|drawing|drawn|fan\s*art|sketch|doodle)\b",
    re.IGNORECASE,
)
# "another one" / "lagi" — a follow-up has no subject words of its own, so
# there the model's kind/query (carried over from the previous card) is trusted.
_FOLLOWUP_RE = re.compile(
    r"\b(another|more|again|next|one\s+more|different|lagi|lainnya|yang\s+lain|ganti)\b",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"\b(give|send|show|find|get|post|me|us|a|an|the|some|any|of|please|pls|can|could|you|"
    r"image|images|picture|pictures|pic|pics|photo|photos|gambar|foto|kasih|kirim|tolong|dong|"
    r"minta|cariin|carikan|aku|ku|yang)\b",
    re.IGNORECASE,
)


def _sanitize_media_intent(kind: str, query: str, message_text: str) -> tuple[str, str]:
    """Returns the (kind, query) that should really be used.

    - The user asked for drawn art (or it's a follow-up to one) → trust the model.
    - Otherwise a model-picked cartoon / anime-flavored query is a hallucination
      from chat history → force a real photo and take the subject from the
      user's own message."""
    if kind == "video":
        return kind, query
    user_art = bool(_ART_REQUEST_RE.search(message_text))
    followup = bool(_FOLLOWUP_RE.search(message_text))
    query_art = bool(_ART_REQUEST_RE.search(query))
    if user_art or (followup and (kind == "cartoon" or query_art)):
        return ("cartoon" if kind == "photo" else kind), query
    if kind == "cartoon" or query_art:
        # A query that itself carries anime words was copied from chat history /
        # the examples → the real subject is whatever the user actually typed.
        from_msg = " ".join(_FILLER_RE.sub(" ", message_text).split())
        cleaned = from_msg if query_art else query
        if not cleaned:
            cleaned = " ".join(_ART_REQUEST_RE.sub(" ", query).split())
        print(f"🛡️ ignored model's art pick (kind={kind!r}, query={query!r}), "
              f"user said {message_text!r}; using photo {cleaned!r}")
        return ("photo" if kind == "cartoon" else kind), cleaned
    return kind, query


def _wants_nekos(kind: str, query: str, message_text: str) -> bool:
    """True only for a genuinely anime-flavored request (anime/neko/waifu/...
    in the USER's message, or in the query on a follow-up). kind="cartoon"
    alone is NOT enough anymore: "cartoon city" must not return a neko girl.
    Never for video/vector, the nekos APIs only serve still images/GIFs."""
    if kind in ("video", "vector"):
        return False
    text = f"{query} {message_text}" if _FOLLOWUP_RE.search(message_text) else message_text
    return bool(_NEKOS_TRIGGER_RE.search(text))


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

    message_text = getattr(ctx.message, "content", "") or ""
    kind, query = _sanitize_media_intent(kind, query, message_text)
    if not query:
        return "ERROR: no clear subject. Ask the user sweetly what they'd like a picture of."

    # 🌸 Anime-flavored request → roll the 50/30/20 dice.
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
    "Answer exactly that, in your own voice, using ONLY the PROFILE above, never invent "
    "anything beyond it. Describing the images is fine, but don't turn them into claims "
    "about the person's personality or character. Be warm and kind; no insults, no guesses "
    "about who they really are. If an image couldn't be viewed or isn't set, say that "
    "honestly. If the profile has NITRO_FLAG / DECO_FLAG lines, you may react to them "
    "playfully in character (e.g. treat them like a fancy Nitro member, 'ooh Nitro vibes' / "
    "'that a_ animated pfp is so fancy'); a NITRO_FLAG is a strong hint, a DECO_FLAG alone is "
    "not proof of Nitro, and never state that they are rich or comment on their money."
)

_PROPS_RULES = (
    "The user wants something about this member (fact, joke, light roast, compliment, trivia, "
    "rating, comparison, info). Use ONLY the PROPS above: never invent anything, never claim to "
    "know their personality, hobbies, messages or private life. Playful teasing about the stats "
    "(join date, account age, roles, badges, boosting, 'ancient account', 'collects roles like "
    "trophies') is welcome but warm: no real insults, no jokes about appearance, identity, "
    "religion, health, family or money. Quote the exact numbers/dates. 1-3 short sentences unless "
    "asked for more, in your usual voice. If PROPS don't cover the ask, say sweetly you can't see that."
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
                "PROFILE (you can't see images, this is a description of what is there):\n"
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
        "This lookup tells you NOTHING about their personality or looks, if the user also "
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
    """🌸 'is my pfp/profile cool', the asker wants THEIR OWN profile looked at. True when the
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
    …/avatar-decoration-presets/a_<hash>.png), never LIKE '%a_%' in SQL,
    where _ is a wildcard and everything matches."""
    if not url:
        return False
    last = str(url).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return last.startswith("a_")


def _still_image_url(url: str, size: int = 512) -> str:
    """🌸 Vision-friendly CDN link: animated hashes (a_…) get a .png (first
    frame, Discord serves that), and the size is capped so the image
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
        f"A Discord user's {labels} image(s), in that order"
        + (" (DECORATION = a frame/effect around the avatar: border, flames, sparkles)"
           if decoration_url else "")
        + ". For each write ONE line, max 25 words: subject, art style, main colors, mood, any "
        "readable text. Describe only what is visible, do NOT identify real people or guess "
        "age, ethnicity or who they are. Format exactly:\n"
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
        print(f"⚠️ Profile image vision returned empty content ({PROFILE_VISION_MODEL}), "
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

    unviewable = "exists but couldn't be viewed right now, don't guess what it shows"
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
            "NITRO_FLAG accent_colour: custom accent colour = Nitro feature, so most likely a "
            "Nitro member."
        )
    if has_avatar and _is_animated_hash(avatar):
        flags.append(
            "NITRO_FLAG animated_pfp: animated GIF pfp (hash starts a_) needs Nitro, they "
            "definitely have/had it."
        )
    if has_deco and _is_animated_hash(deco):
        flags.append(
            "DECO_FLAG animated_decoration: animated avatar frame (Discord Shop, cheaper/free "
            "with Nitro), a premium flex, NOT proof of Nitro."
        )
    elif has_deco:
        flags.append(
            "DECO_FLAG static_decoration: static Discord Shop avatar frame, premium cosmetic, "
            "NOT proof of Nitro."
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


# 🌸 'Fun fact about him' = a RANDOM fun fact dedicated to them, NOT Discord stats. The member
# is still resolved (so the reply names the right person) but no PROPS block is built.
_FUNFACT_TOKENS = frozenset({"funfact", "fact", "facts", "fun", "trivia"})
_NON_FUNFACT_TOKENS = frozenset({
    "props", "prop", "properties", "info", "joke", "jokes", "roast", "compliment",
    "stats", "details", "about",
})
_FUNFACT_RULES = (
    "FUN FACT request: do NOT use their Discord stats (join date, account age, roles, badges, "
    "boosting). Give ONE random, true, quirky general fun fact (animals, space, food, science, "
    "history, words), dedicated to them in 1-2 short sentences and your cutest voice. For the "
    "joke twist you may playfully tease their NAME (a word in it, its symbols, how it looks or "
    "sounds). Keep any teasing light and warm: no real insults, nothing about their body, face, "
    "identity, religion, health, family or money. Never present the fact as true about their real "
    "life, and never invent their personality, hobbies or private details."
)
# 🌸 Added ONLY when a PROFILE block (pfp/banner description) is attached to a fun fact request.
_FUNFACT_PROFILE_RULES = (
    "A PROFILE is attached: you may also joke about their avatar, banner or decoration, built ONLY "
    "from the PROFILE text (colours, objects, vibe, Nitro flags). Tease the pictures and the name, "
    "never the person. If an image couldn't be viewed, say so instead of guessing."
)


def _funfact_with_profile(body: str, profile_block: str) -> str:
    """🌸 Fun fact + joke about name/avatar/banner: the lookup line, the PROFILE description,
    then the fun fact rules plus the profile-teasing rules."""
    return (
        f"LOOKUP RESULT: {body}\n"
        "PROFILE (you can't see images, this is a description of what is there):\n"
        f"{profile_block}\n"
        f"({_FUNFACT_RULES} {_FUNFACT_PROFILE_RULES} Keep the <@id> mention exactly as written.)"
    )


def _wants_funfact(args: dict) -> bool:
    intent = str(args.get("intent") or "").strip().lower()
    toks = {x for x in re.split(r"[^a-z]+", intent) if x}
    return bool(toks & _FUNFACT_TOKENS) and not (toks & _NON_FUNFACT_TOKENS)


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
    shows on a profile, no message history, nothing private."""
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
    """🌸 The asker's OWN profile ('is my pfp cool'): no name lookup at all, the author's ID
    goes straight to members.db, so '@Bruh' (the bot) can never be picked up as the target.
    want_props also adds their server properties ('roast my pfp and join date')."""
    author = ctx.message.author
    profile_block = await _profile_block(ctx, str(author.id), user_id=author.id)
    props_block = await _member_props_block(ctx, author.id) if want_props else None
    result = (
        f"✅ Yep, <@{author.id}> is here, that's the person asking, and they asked about THEIR OWN "
        "profile (say 'your', not 'their')."
    )
    _remember_member_result(ctx.message.id, result)
    out = _member_result_for_model(result, profile_block, props_block)
    if not profile_block:
        out += (
            "\n(Their profile couldn't be loaded right now, tell them sweetly you can't see it, "
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
        if _wants_funfact(args):
            self_body = f"<@{ctx.message.author.id}> is the person asking (say 'you', not 'they')."
            if _EXPLICIT_PROFILE_RE.search(getattr(ctx.message, "content", "") or ""):
                self_profile = await _profile_block(ctx, str(ctx.message.author.id), user_id=ctx.message.author.id)
                if self_profile:
                    return _funfact_with_profile(self_body, self_profile)
            return (
                f"LOOKUP RESULT: {self_body}\n"
                f"({_FUNFACT_RULES} Keep the <@id> mention exactly as written.)"
            )
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
        msg_text = getattr(ctx.message, "content", "") or ""
        funfact = found and _wants_funfact(args)
        # 🌸 A fun fact with 'make fun of his avatar/banner' also reads the profile, behind the
        # same hard gate as intent='profile': the user's OWN words must name pfp/avatar/banner/looks.
        if found and (_wants_profile(args, msg_text)
                      or (funfact and _EXPLICIT_PROFILE_RE.search(msg_text))):
            profile_block = await _profile_block(ctx, target)
        # 🌸 intent='props' → real server properties for facts/jokes/roasts/trivia.
        # The member id comes from the lookup's own '<@id>' hit, so it can only
        # ever describe the ONE member the handler actually found.
        if found and not funfact and _wants_props(args):
            member_id = _member_id_from_result(result)
            if member_id:
                props_block = await _member_props_block(ctx, member_id)
        if funfact:
            body = re.sub(r"^[\s✅❌]+", "", result).strip()
            if profile_block:
                result = _funfact_with_profile(body, profile_block)
            else:
                result = f"LOOKUP RESULT: {body}\n({_FUNFACT_RULES} Keep the <@id> mention exactly as written.)"
        else:
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


_RANDOM_PICK_MAX = 10
_RANDOM_PROPS_MAX = 3            # picks that get a props block (fact/joke) per call
_RANDOM_RESULT_MAX_CHARS = 5200  # room for up to 3 props blocks + stats + rules
_ROLE_ID_ARG_RE = re.compile(r"^\s*<?@?&?(\d{17,20})>?\s*$")


def _clamp_count(value, default: int = 1, maximum: int = _RANDOM_PICK_MAX) -> int:
    """🌸 Models send count as 3, "3", 3.0 or garbage, always land on 1..maximum."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


_OPINION_RULES = (
    "For YOUR opinion (favorite member, who you like, best/cutest/coolest): pick ONE person from "
    "the SERVER STATS above, not just the random pick, and say it as your personal favorite, in "
    "your usual voice. Praise the elder for staying longest, admire the role collector's trophy "
    "shelf, or gush over the profile-style star's aesthetic (best for cute/pretty questions). If "
    "'What their profile looks like' is given, it was read from the real images: rate the ART "
    "(style, colours, mood, banner, frame), never anyone's face or body; a photo of a person is "
    "just 'a photo', talk colours/vibe. Without that line you can't see the images (the accent "
    "colour's vibe is fair game). Cite the exact dates/numbers/features. You know nothing else "
    "about them: no invented personality, hobbies, messages or private life; stay warm, never "
    "insulting. For a plain random pick or a fact/joke about it (PROPS above), ignore the STATS and "
    "follow that request."
)


# 🌸 Opinion questions ('who is your favorite member?', 'who is the cutest?') are the ONLY
# pick_random_member calls that spend a vision call on the profile-style winner's avatar /
# banner. Either the model says intent='opinion', or the user's own words are opinion-ish
# (the model often forgets the intent, same reason _wants_profile has a regex gate).
_OPINION_INTENTS = ("opinion", "favorite", "favourite", "best", "rate", "rating")
_OPINION_RE = re.compile(
    r"\b(fav(?:ou?rite)?s?|best|cutest|prettiest|coolest|hottest|handsomest|most\s+\w+|aesthetic|"
    r"who\s+do\s+you\s+(?:like|love|prefer)|which\s+(?:member|one)\s+do\s+you\s+(?:like|love|prefer)|"
    r"terbaik|favorit|paling\s+\w+|imut|cantik|keren|lucu|suka)\b",
    re.IGNORECASE,
)


def _wants_opinion(args: dict, message_text: str = "") -> bool:
    intent = str(args.get("intent") or "").strip().lower()
    return intent.startswith(_OPINION_INTENTS) or bool(_OPINION_RE.search(message_text or ""))


def _color_vibe(hex_color: str | None) -> str:
    """🌸 '#FFB6C1' -> 'soft pink (pastel, very kawaii)'. Plain words so the model
    can talk about the colour without guessing from a hex code."""
    try:
        h = str(hex_color).strip().lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (TypeError, ValueError, IndexError):
        return "an unreadable colour"
    hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
    deg = hue * 360
    if val < 0.2:
        return "near-black"
    if sat < 0.12:
        return "white" if val > 0.85 else "grey"
    if deg < 15 or deg >= 345:
        name = "red"
    elif deg < 45:
        name = "orange"
    elif deg < 70:
        name = "yellow"
    elif deg < 165:
        name = "green"
    elif deg < 200:
        name = "cyan"
    elif deg < 255:
        name = "blue"
    elif deg < 290:
        name = "purple"
    else:
        name = "pink"
    pastel = sat < 0.55 and val > 0.8
    if name == "red" and sat < 0.65 and val > 0.7:
        name = "pink"  # light/pastel reds are pinks (#FFB6C1), not 'soft red'
    if pastel:
        name = f"soft {name}"
    kawaii = name.endswith(("pink", "purple")) or pastel
    tags = [t_ for t_ in ("pastel" if pastel else "", "very kawaii" if kawaii else "") if t_]
    return f"{name} ({', '.join(tags)})" if tags else name


async def _server_stats_block(ctx: _Ctx, look: bool = False) -> str | None:
    """🌸 Real statistics for opinion questions, all straight from the databases via
    shared.py: the most senior member (earliest joined_at), the biggest role collector
    (roles.db) and the best profile style (members.db: avatar / banner / accent colour /
    decoration). Each lookup fails soft; None when none of them has data.
    look=True (opinion questions) also has PROFILE_VISION_MODEL read the style winner's
    avatar / banner / decoration, so the rating can talk about what is really in them."""
    senior = collector = styled = None
    try:
        senior = await ctx.shared.get_top_senior_member(ctx.guild_id)
    except Exception as e:
        print(f"⚠️ Senior-member stat failed ({type(e).__name__}): {e}")
    try:
        collector = await ctx.shared.get_top_role_collector(ctx.guild_id)
    except Exception as e:
        print(f"⚠️ Role-collector stat failed ({type(e).__name__}): {e}")
    try:
        styled = await ctx.shared.get_top_profile_style_member(ctx.guild_id)
    except Exception as e:
        print(f"⚠️ Profile-style stat failed ({type(e).__name__}): {e}")

    lines = []
    if senior:
        try:
            joined = datetime.fromisoformat(str(senior["joined_at"]).strip())
            when = f"joined {_fmt_date(joined)} ({_humanize_age(joined)} ago)"
        except (TypeError, ValueError):
            when = f"joined {senior['joined_at']}"
        lines.append(
            f"- Most senior member (earliest to join): <@{senior['id']}> "
            f"({senior['display_name'] or senior['username']}), {when}"
        )
    if collector:
        lines.append(
            f"- Biggest role collector (most roles): <@{collector['id']}> "
            f"({collector['display_name'] or collector['username']}), "
            f"{collector['role_count']} role(s)"
        )
    if styled:
        feats = []
        if styled["custom_avatar"]:
            feats.append("an animated custom avatar" if styled["animated_avatar"] else "a custom avatar")
        if styled["has_banner"]:
            feats.append("a profile banner")
        if styled["accent_color"]:
            feats.append(
                f"an accent colour {styled['accent_color']}, {_color_vibe(styled['accent_color'])}"
            )
        if styled["has_decoration"]:
            feats.append("an avatar decoration")
        lines.append(
            f"- Best profile style (avatar/banner/accent colour): <@{styled['id']}> "
            f"({styled['display_name'] or styled['username']}), "
            f"{styled['style_score']}/5 style points: " + ", ".join(feats)
        )
        if look:
            descs = await _describe_profile_images(
                ctx,
                styled["avatar_url"] if styled["custom_avatar"] else None,
                styled["banner_url"],
                styled["decoration_url"],
            )
            if descs:
                seen = "; ".join(f"{k}: {v}" for k, v in descs.items())
                lines.append(
                    f"  What their profile looks like (read from the images by a vision model): {seen}"
                )
            else:
                lines.append(
                    "  (The images couldn't be viewed right now, rate only from the features above.)"
                )
    if not lines:
        return None
    ids = [x["id"] for x in (senior, collector, styled) if x]
    if len(ids) > 1 and len(set(ids)) == 1:
        lines.append("(The same person leads every stat, a triple champion!)" if len(ids) == 3
                     else "(The same person leads both stats, a double champion!)")
    return "SERVER STATS (real data from the database, non-bot members only):\n" + "\n".join(lines)


async def _exec_random_member(ctx: _Ctx, args: dict) -> str:
    if ctx.message.guild is None:
        return "This only works inside a server, not in DMs."

    count = _clamp_count(args.get("count"))
    role = str(args.get("role_name") or "").strip().lstrip("@")
    role_id = None
    id_match = _ROLE_ID_ARG_RE.match(role) if role else None
    if id_match:
        role_id = int(id_match.group(1))

    # 🌸 ORDER BY RANDOM() LIMIT ? happens inside SQLite (shared.get_random_members).
    members = await ctx.shared.get_random_members(
        ctx.guild_id, count, role_name=role or None, role_id=role_id,
    )
    scope = f" with the role '{role}'" if role and not role_id else (" with that role" if role_id else "")
    if not members:
        return (
            f"No members found{scope}. Tell the user sweetly nobody could be picked (role missing, "
            "empty, or data not synced), never invent a name."
        )

    lines = [f"- <@{m['id']}> ({m['display_name'] or m['username']})" for m in members]
    note = ""
    if len(members) < count:
        note = f"\n(Only {len(members)} available{scope}, fewer than the {count} asked.)"
    result = (
        f"🎲 Random pick ({len(members)}){scope}, chosen by the database. For a plain pick "
        "request announce exactly these, no swaps:\n"
        + "\n".join(lines) + note
    )

    # 🌸 intent='props' ('random member with a fact and a joke'): attach each pick's REAL
    # server properties (same block check_member_existence builds) so the fact/joke comes
    # from data, not invention. Capped so a 10-member pick can't blow the follow-up call.
    if _wants_funfact(args):
        result += "\n\n(" + _FUNFACT_RULES + " Do it for each pick.)"
    elif _wants_props(args):
        blocks = []
        for m in members[:_RANDOM_PROPS_MAX]:
            block = await _member_props_block(ctx, m["id"])
            if block:
                blocks.append(f"PROPS for <@{m['id']}>:\n{block}")
        if blocks:
            result += (
                "\n\n" + "\n\n".join(blocks) + "\n\n(" + _PROPS_RULES
                + " Apply to each pick with PROPS.)"
            )
            if len(members) > _RANDOM_PROPS_MAX:
                result += f"\n(Only the first {_RANDOM_PROPS_MAX} picks got PROPS; just name the rest.)"
        else:
            result += (
                "\n(Properties couldn't load, name the pick(s), say sweetly you can't see more. "
                "No invented facts.)"
            )

    # 🌸 Opinion questions ('who is your favorite member?', 'who is the best/cutest?') get REAL
    # statistics (server elder, role collector, profile style; the style winner's avatar/banner
    # is read by the vision model) so the bot forms its own favorite instead of a blind dice
    # roll. Gated by _wants_opinion (intent or the user's own words) so plain picks and
    # fact/joke requests don't pay for the extra queries, vision call and tokens. It applies
    # on the props path too — the model often adds intent='props' to opinion questions.
    if _wants_opinion(args, getattr(ctx.message, "content", "") or ""):
        stats = await _server_stats_block(ctx, look=True)
        if stats:
            result += "\n\n" + stats + "\n\n(" + _OPINION_RULES + ")"
        else:
            result += (
                "\n(No stats yet: for 'best/coolest/funniest member' questions, crown the random "
                "pick playfully and warmly, no invented personality.)"
            )
    return _clip(_with_mention_note(result), _RANDOM_RESULT_MAX_CHARS)


async def _exec_random_role(ctx: _Ctx, args: dict) -> str:
    if ctx.message.guild is None:
        return "This only works inside a server, not in DMs."

    count = _clamp_count(args.get("count"))
    roles = await ctx.shared.get_random_roles(ctx.guild_id, count)
    if not roles:
        return (
            "No roles found. Tell the user sweetly none could be picked now (data may not be "
            "synced), never invent a role."
        )

    lines = [f"- <@&{r['id']}> ({r['name']}, {r['member_count']} member(s))" for r in roles]
    note = ""
    if len(roles) < count:
        note = f"\n(Only {len(roles)} available, fewer than the {count} asked.)"
    result = (
        f"🎲 Random pick ({len(roles)}), chosen by the database, announce exactly these, no "
        "swaps:\n" + "\n".join(lines) + note
    )
    return _clip(_with_mention_note(result), _MEMBER_RESULT_MAX_CHARS)


async def _exec_dm(ctx: _Ctx, args: dict) -> str:
    if ctx.message.guild is None:
        return "This is already a DM, so there's nothing to send privately, just reply normally."
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
    "pick_random_member": _exec_random_member,
    "pick_random_role": _exec_random_role,
    "send_dm_to_user": _exec_dm,
    "decode_discord_snowflake": _exec_snowflake,
}


async def run_tool_calls(service, response_message, *, message, shared_ctx) -> tuple[list, bool, list, list]:
    """🌸 Executes every tool call in one model response.

    Returns (tool_messages, delivered, pending, private):
      tool_messages, one {"role": "tool", "tool_call_id": ..., "content": ...}
                      per call, in order, ready to extend into `messages`.
      delivered    , True if any tool queued media/music for the channel,
                      so the caller can switch tools off for the follow-up
                      and avoid a duplicate post.
      pending      , the queued async sends (see _deliver). NOT run here:
                      they must go out only after the model's text reply
                      has been sent, so the card appears below it.
      private      , texts sent to the user's DMs this round; completion.py
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
