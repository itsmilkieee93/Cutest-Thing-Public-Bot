"""🌸 Prompt-assembly module — extracted from groq_ai.py's monolithic
get_ai_response. Structure-only split: same model-pick order (explicit
model_id wins; else classify_search_intent then _wants_web_search; Exa
first; on Exa hit use non-compound MODEL_POOL; else groq/compound-mini;
else random MODEL_POOL), same personality/identity/react/DM/server/Exa/
anchor assembly, same guild_summary skip for compound calls.

Two entry points, called in order from engine/service.py's
get_ai_response orchestrator:

  pick_model_and_search(service, prompt, username, model_id)
      → (model_to_use, exa_context, is_compound_call)

  build_system_prompt(service, ..., is_compound_call, exa_context, ...)
      → personality (the fully assembled system-prompt string)

Kept as two functions (not one) because is_compound_call is needed by
BOTH model pick (to know if Exa hit changes model_to_use) and prompt
assembly (to skip server_context/guild_summary) — the orchestrator
threads it through explicitly rather than recomputing it twice.
"""
import random

from groq_instruct import (
    MODEL_POOL, REACT_EMOJI_POOL, REACT_INSTRUCTIONS_DISALLOWED,
    IDENTITY_INSTRUCTIONS, DM_CONTEXT_INSTRUCTIONS,
    _build_react_instructions, _build_owner_status,
    _wants_web_search,
)
from extras.groq_dm_instruct import build_dm_directives, format_snowflake_info
from personality import get_personality_for_nickname

from resources import shared


async def pick_model_and_search(service, prompt: str, username: str, model_id: str = None):
    """
    🌸 Random model pick per turn (unless caller pinned model_id) —
    see MODEL_POOL up top for what's in rotation. EXCEPTION: if the
    prompt looks like a search request, try Exa FIRST (see
    ExaSearchService in groq_exa_search.py) instead of forcing
    groq/compound-mini straight away. Exa does its own
    search+summarize outside of Groq entirely and hands back a
    short synthesized string, which sidesteps the whole
    compound/browser_search 413 saga at the root — that was always
    caused by raw Tavily search output sharing a TPM budget with
    the SAME Groq call generating the reply. If Exa is unavailable
    (no key, exa_py not installed, network blip, empty result) we
    fall straight back to groq/compound-mini, and the existing
    slim-retry → browser_search → no-search fallback chain further
    down still applies exactly as before.

    🌸 AI-FIRST, REGEX 2ND: search intent is now decided by
    classify_search_intent (one cheap Groq call, same shape as
    classify_server_query) so paraphrases the regex net would miss
    ("did they release the sequel yet", "how's Bitcoin doing rn")
    still trigger Exa. _wants_web_search's SEARCH_INTENT_PATTERN
    regex is kept as the FALLBACK — only consulted when the
    classifier call itself fails/errors/times out (classify_search_intent
    already returns False in that case, which would otherwise look
    identical to a genuine "no search needed" verdict) — never
    removed, only skipped when the AI call already succeeded.
    Caller-pinned model_id (e.g. from an explicit /ask-with-model
    command) still wins over all of this — we only override the
    RANDOM/search-intent pick, never an explicit one.

    Returns (model_to_use, exa_context, is_compound_call).
    """
    exa_context = None
    if model_id:
        model_to_use = model_id
    else:
        wants_search = await service.classify_search_intent(prompt, username)
        if not wants_search:
            wants_search = _wants_web_search(prompt)

        if wants_search:
            if service.exa:
                exa_context = await service.exa.search(prompt)
            if exa_context:
                non_compound_pool = [m for m in MODEL_POOL if "compound" not in m.lower()]
                model_to_use = random.choice(non_compound_pool) if non_compound_pool else service.default_model
            else:
                model_to_use = "groq/compound-mini"
        else:
            model_to_use = random.choice(MODEL_POOL)

    # 🌸 COMPOUND TOKEN BUDGET (part 2): server_context/guild_summary
    # can be genuinely huge on an active server — full role/channel/
    # member dumps easily run several thousand tokens on their own.
    # That's fine for gpt-oss/llama/qwen (large context, no extra
    # tool overhead), but compound/compound-mini ALSO pays token cost
    # for the search tool's query + returned Tavily snippets on top
    # of whatever system prompt we send, and a thin TPM budget can't
    # absorb both. Search questions ("who's winning the world cup",
    # "weather in Jakarta") essentially never need server role/channel
    # data anyway, so just skip building it for compound calls —
    # cheaper AND avoids pulling the model's attention toward
    # server trivia instead of doing the actual search.
    is_compound_call = "compound" in model_to_use.lower()

    return model_to_use, exa_context, is_compound_call


async def build_system_prompt(
    service,
    *,
    prompt: str,
    username: str,
    user_id: int,
    display_name: str,
    global_name: str,
    guild_nickname: str,
    react_allowed: bool,
    dm_requested: bool,
    guild,
    recent_react_emoji,
    is_compound_call: bool,
    exa_context,
    reply_to_message_id,
    tone_hint: str = "",
) -> str:
    """
    🌸 Builds the full system-prompt string ("personality") sent to
    Groq: dynamic per-guild personality, react directives, DM
    split-route directives, identity block (display/global/nickname +
    snowflake decode + owner status), server context (guild
    role/channel summary or DM context), the "ignore old data"
    server_override note, live Exa search grounding when present, and
    the reply-anchor instruction when this turn is anchored to an old
    bot message. Byte-for-byte the same assembly/order as the
    original inline get_ai_response body.
    """
    # 🌸 Personality now follows the bot's CURRENT per-guild nickname
    # (set via /server-persona-set) instead of a static file — read
    # fresh every call so a nickname change takes effect on the very
    # next reply, with zero code changes needed for new personas.
    # guild.me is None in a DM, so nickname falls back to the default
    # inside get_personality_for_nickname().
    nickname = guild.me.nick if guild and guild.me else None
    try:
        personality = get_personality_for_nickname(nickname, tone_hint)
    except Exception as e:
        # 🌸 Last-resort fallback if personality.py errors out for
        # any reason — keeps the bot answering instead of crashing.
        print(f"⚠️ Dynamic personality load failed, falling back to file: {e}")
        personality = service._load_file(service.personality_path)

    # 🌸 Snowflake decode is pure local math (see
    # extras/groq_dm_instruct.decode_snowflake) — never fails on a
    # valid Discord ID, but wrapped anyway so a malformed/None
    # user_id from some non-standard caller can't take down the
    # whole reply over an identity nicety.
    try:
        snowflake_info = format_snowflake_info(user_id)
    except Exception as e:
        print(f"⚠️ Snowflake decode failed for user_id={user_id}: {e}")
        snowflake_info = f"ID {user_id} (creation date unavailable)"

    identity = IDENTITY_INSTRUCTIONS.format(
        display_name=display_name or username,
        username=username,
        global_name=global_name or "(not set)",
        guild_nickname=guild_nickname or "(no nickname set here)",
        snowflake_info=snowflake_info,
        owner_status=_build_owner_status(user_id),
    )
    if react_allowed:
        # 🌸 Pick a random example emoji that ISN'T one of this
        # channel's recent auto-reacts, so the example itself nudges
        # Groq away from repeating — then also spell out the avoid-list
        # explicitly (see _build_react_instructions).
        avoid_set = set(recent_react_emoji or [])
        example_pool = [e for e in REACT_EMOJI_POOL if e not in avoid_set] or REACT_EMOJI_POOL
        example_emoji = random.choice(example_pool)
        react_directives = _build_react_instructions(example_emoji, recent_react_emoji)
    else:
        react_directives = REACT_INSTRUCTIONS_DISALLOWED

    # 🌸 DM split-routing — only offered in a GUILD (there's a public
    # channel + a private DM to split between there). In a DM, guild
    # is None and "send this to their DMs" is meaningless since
    # they're already IN their DMs. `dm_requested` (computed by the
    # caller from DM_REQUEST_PATTERN — same shape as react_allowed
    # from REACT_REQUEST_PATTERN) strengthens the instruction on
    # turns where the user explicitly asked, instead of leaving
    # detection entirely up to the model's own judgement. See
    # extras/groq_dm_instruct.build_dm_directives for the full logic.
    dm_directives = build_dm_directives(dm_allowed=bool(guild), dm_requested=dm_requested)

    # 🌸 Server context — pulled from the v10 REST API and cached (see
    # GroqMentionService.get_server_context_text in groq_service.py) so
    # Groq always knows what server it's replying in, same idea as
    # IDENTITY_INSTRUCTIONS but for "where" instead of "who". Falls
    # back to DM_CONTEXT_INSTRUCTIONS when guild is None, or "" if
    # there's no bot back-reference at all (e.g. running this service
    # standalone outside EnchantedBot).
    if is_compound_call:
        server_context = ""
    elif guild is None:
        # 🌸 DMs have no guild at all — get_server_context_text (and
        # self.guild_info_cache inside it) is guild-shaped (guild.id/
        # guild.name/guild.member_count), so it must never be called
        # with guild=None. This branch has to come BEFORE the
        # self.bot.groq_mentions check below, since that check was
        # true even in DMs (groq_mentions exists regardless of
        # channel type) and was routing DMs into
        # get_server_context_text(None) anyway, crashing on
        # guild.id — see the fallback that was meant to catch this.
        server_context = DM_CONTEXT_INSTRUCTIONS
    elif service.bot and getattr(service.bot, "groq_mentions", None):
        server_context = service.bot.groq_mentions.get_server_context_text(guild)
    else:
        server_context = ""

    # 🌸 Pull compact guild info straight from the per-guild SQLite
    # cache (metadata.db/roles.db/channels.db) instead of dumping the
    # full guild JSON into the prompt. shared.get_guild_context_summary
    # already does the GROUP BY queries + formatting — just await it.
    guild_summary = ""
    if guild and not is_compound_call:
        try:
            guild_summary = await shared.get_guild_context_summary(guild.id)
        except Exception as e:
            print(f"❌ guild_summary fetch error ({guild.id}): {e}")
            guild_summary = ""

    # 🌸 Explicit "ignore old data" instruction — when answering questions about
    # channels/server info, use ONLY the current server data below, not channel
    # names from old conversations in OTHER servers (which may be in chat history).
    server_override = (
        "⚠️ IMPORTANT: When answering questions about channels, server info, or what exists here, "
        "use ONLY the current server information below. Ignore any channel names from past "
        "conversations — they may be from a different server. When listing channels, format them "
        "as a comma-separated list on a single line (e.g., '#general, #announcements, #off-topic') "
        "or in a compact table, NOT as bullet points. The CURRENT server is:"
    ) if guild and guild_summary else ""

    personality = f"{personality}\n\n{react_directives}\n\n{dm_directives}\n\n{identity}\n\n{server_context}\n\n{server_override}\n{guild_summary}".strip()

    # 🌸 EXA SEARCH RESULT — only present when the model-selection
    # block above got a live answer back from Exa for this prompt
    # (see exa_context up top). Injected into the SYSTEM prompt, not
    # the user-facing `prompt` itself, so it never gets saved into
    # Groq memory / bloats future turns' history — it's a one-turn
    # ephemeral grounding, same treatment as guild_summary above.
    if exa_context:
        personality = (
            f"{personality}\n\n"
            "🌸 LIVE SEARCH RESULT (via Exa, fetched just now for this question):\n"
            f"{exa_context}\n\n"
            "Use the above to answer accurately and in your own words/persona — don't just "
            "repeat it verbatim, and don't contradict it with outdated knowledge."
        )

    if reply_to_message_id is not None:
        # 🌸 Only added when the user actually swiped-replied to an old
        # bot message — tells Groq how to read the [REPLYING TO THIS]
        # flags that may appear in recent_history below. Covers BOTH
        # cases: a real anchored exchange found in memory, and the
        # FALLBACK CONTEXT pseudo-turn appended when the replied-to
        # message was an interceptor reply never saved to history
        # (see the slicing block below). Either way, by the time this
        # instruction matters a flagged turn is present in
        # recent_history — if somehow neither fired (e.g. the
        # replied-to message had zero extractable text), this is just
        # harmless unused instruction text.
        anchor_instructions = (
            "⚠️ The user replied directly to one specific earlier message, marked with "
            "[REPLYING TO THIS ⬇️] and [THIS IS THE MESSAGE BEING REPLIED TO] tags below. "
            "Treat THAT exchange as the primary context for their current message — not "
            "whatever else appears nearby in the history."
        )
        personality = f"{personality}\n\n{anchor_instructions}"

    return personality
