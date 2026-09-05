"""🌸 extras/groq_dm_instruct.py

DM SPLIT-ROUTING — everything for the "AI autonomously decides to send
part of its reply to the user's DMs instead of the public channel"
feature lives here, split out of groq_instruct.py/groq_service.py so
this one feature (tag pattern, request detection, instruction text,
and the actual send/error-handling orchestration) has a single home
instead of being smeared across three files.

Mirrors the existing [REACT:...] tag feature's shape on purpose:
  - DM_TAG_PATTERN is parsed and stripped the same way REACT_TAG_PATTERN
    is in groq_service.py's reply pipeline.
  - DM_REQUEST_PATTERN gates/strengthens the instruction the same way
    REACT_REQUEST_PATTERN gates react_allowed — AI (the tag, which
    content to wrap) and regex (reliably catching "dm me"/"kirim ke dm"
    style explicit asks even if the model doesn't judge it as
    "sensitive enough" on its own) work TOGETHER rather than either one
    alone deciding everything.

Nothing in this module imports groq_ai.py or holds a Groq client — the
one place this feature needs an actual AI call (phrasing the success/
privacy-blocked notice in-character) is injected as a callback
(`ai_notice`) from the caller, which already has a live GroqService.
That keeps this module import-safe from both groq_ai.py and
groq_service.py without any circular-import juggling.
"""

import re
from datetime import datetime, timezone
import discord

# 🌸 DISCORD SNOWFLAKE DECODER — every Discord ID (user, message, channel,
# guild...) is a 64-bit snowflake with the creation timestamp baked into
# the top 42 bits, offset from the Discord Epoch (2015-01-01T00:00:00Z),
# NOT the Unix Epoch. No API call, no "go paste it into discord.dog" — the
# math is public and instant, so the bot can just answer this itself.
# Reference: https://discord.com/developers/docs/reference#snowflakes
DISCORD_EPOCH_MS = 1420070400000  # 2015-01-01T00:00:00.000Z in Unix ms


def decode_snowflake(snowflake: int) -> datetime:
    """🌸 Extracts the creation timestamp baked into ANY Discord snowflake
    (user ID, message ID, channel ID, guild ID — the format is identical
    for all of them). Returns a timezone-aware UTC datetime.

    Math: the first 42 bits of the 64-bit integer are milliseconds since
    the Discord Epoch — shift right 22 bits to drop the worker/process/
    increment bits, then add back the epoch offset.
    """
    timestamp_ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def format_snowflake_info(snowflake: int) -> str:
    """🌸 Human/AI-readable one-liner combining the raw ID with its
    decoded creation date — this is what gets fed into the identity
    context so Groq can just STATE the answer instead of telling the
    user to go run Dev Mode + a third-party decoder site themselves.
    """
    created = decode_snowflake(snowflake)
    return (
        f"ID {snowflake} was created "
        f"{created.strftime('%B %d, %Y at %H:%M:%S UTC')}"
    )

# 🌸 Same "emit a bracketed tag, strip it server-side before the public
# message goes out" shape as REACT_TAG_PATTERN in groq_instruct.py.
# DOTALL so a multi-line secret/code block still gets captured whole.
DM_TAG_PATTERN = re.compile(r"\[DM_START\](.*?)\[DM_END\]", re.DOTALL | re.IGNORECASE)

# 🌸 Detects the user EXPLICITLY asking to be DM'd — English and
# Indonesian phrasing both covered ("dm me", "send it to my dm(s)",
# "kirim ke dm", "dm dong", "say something in my dm", "pm me"). Same
# role as REACT_REQUEST_PATTERN: this is what lets groq_service.py tell
# Groq "they just asked FOR REAL this turn, you MUST use the tag"
# instead of leaving detection entirely up to the model's own judgement
# every time.
#
# Deliberately requires "dm"/"pm" to be paired with a verb/pronoun/
# preposition ("dm me", "pm me", "in my dm", "send ... dm(s)") rather
# than matching the bare word "dm" anywhere — a message like "I like dm
# radio stations" or "check my dm history channel" should NOT trigger
# this even though "dm" appears in it.
DM_REQUEST_PATTERN = re.compile(
    r"\b(dm|pm)\s*(me|dong|please|plz|pls)\b"
    r"|\bme\s*(dm|pm)\b"
    r"|\bin\s+(my|ur|your)\s+(dm|dms|pm|pms)\b"
    r"|\b(send|kirim)\w*\b.*\b(dm|dms|pm|pms|private(?:ly)?|privat)\b",
    re.IGNORECASE,
)

# 🌸 Base instruction — offered every turn in a guild (see
# groq_ai.get_ai_response's dm_directives assignment). Covers the
# "content just happens to be sensitive, no one asked" case as well as
# a soft mention of explicit asks; DM_INSTRUCTIONS_REQUESTED below is
# layered on TOP of this (not a replacement) on turns where
# DM_REQUEST_PATTERN actually fired, to make the ask un-ignorable.
DM_INSTRUCTIONS = (
    "If — and ONLY if — the user explicitly asks you to send something to "
    "their DMs (e.g. \"DM me that\", \"kirim ke DM dong\", \"send it "
    "privately\") OR the content you're about to share is genuinely "
    "sensitive (a secret, a coupon/redeem code, a password, personal "
    "search results they wouldn't want visible to the whole server), wrap "
    "ONLY that private part in [DM_START] and [DM_END] tags. Everything "
    "OUTSIDE the tags is what stays in the server channel, and it must "
    "still read as a complete, natural reply on its own — but DO NOT "
    "claim or imply the DM was already sent/delivered in that public "
    "text (e.g. never say \"sent it to your DMs!\" yourself) — whether "
    "delivery actually succeeds is decided AFTER you respond, and a "
    "separate, accurate notice is sent for that outcome. Just react "
    "normally/casually in the public part, like \"on it!\" or a relevant "
    "emoji. Put the actual sensitive content ONLY inside the tags, never "
    "duplicated outside them. Use this rarely — most replies need no tag "
    "at all. Max one [DM_START]...[DM_END] block per reply."
)

# 🌸 Layered ON TOP of DM_INSTRUCTIONS (appended, not swapped in) on any
# turn where DM_REQUEST_PATTERN matched the user's message — see
# groq_service.py's dm_requested computation. Makes an explicit ask
# effectively mandatory instead of "the model may judge this counts".
DM_INSTRUCTIONS_REQUESTED = (
    "They just explicitly asked to be DM'd right now — you MUST use the "
    "[DM_START]/[DM_END] tag this turn for whatever they asked to receive "
    "privately. Do not skip it."
)

# 🌸 Used on every turn where DM-splitting isn't offered at all (DMs
# themselves, where public/private is meaningless). Same "explicitly say
# no" reasoning as REACT_INSTRUCTIONS_DISALLOWED — without this, past
# turns in recent_history could still contain real [DM_START]/[DM_END]
# tags (from earlier server messages resurfacing in memory) for the
# model to imitate out of habit.
DM_INSTRUCTIONS_DISALLOWED = "No [DM_START]/[DM_END] tag this time — this is a DM already."


def build_dm_directives(dm_allowed: bool, dm_requested: bool) -> str:
    """🌸 Single entry point groq_ai.get_ai_response calls to build the
    DM-related block of the system prompt, so the "which constant(s) for
    which situation" branching lives in ONE place instead of being
    reimplemented at the call site.

    dm_allowed:   True only in a guild (see DM_INSTRUCTIONS_DISALLOWED's
                  docstring for why DMs themselves never offer this).
    dm_requested: True when DM_REQUEST_PATTERN matched the user's
                  message this turn (computed by the caller, same shape
                  as user_asking_for_react in groq_service.py).
    """
    if not dm_allowed:
        return DM_INSTRUCTIONS_DISALLOWED
    if dm_requested:
        return f"{DM_INSTRUCTIONS}\n{DM_INSTRUCTIONS_REQUESTED}"
    return DM_INSTRUCTIONS


async def route_dm_split(message: discord.Message, server_content: str, ai_notice) -> str:
    """🌸 DM SPLIT-ROUTING — parses an OPTIONAL [DM_START]...[DM_END]
    block out of `server_content` (already past REACT-tag stripping)
    and, if present, delivers that inner text to the requesting user's
    DMs while returning the OUTER text (tagged block removed) for the
    caller to send to the server channel as usual.

    `ai_notice` is an async callback — typically
    GroqService.generate_dm_notice — called as
    `await ai_notice(message, outcome)` where outcome is "sent" or
    "forbidden", and returning a ready-to-send string in the bot's own
    voice/language for that outcome. Injected rather than imported so
    this module never needs a Groq client or personality data of its
    own — see the module docstring.

    Contract, mirroring the existing [REACT:...] parse-then-strip shape:
      - No tag found → server_content is returned completely untouched,
        no DM attempted, ai_notice is never called. This is the
        overwhelmingly common case (DM_INSTRUCTIONS says to use the tag
        rarely).
      - Tag found → the matched inner text is sent via
        `await user.send(...)`, and the block (tags included) is
        stripped from the text this function returns — so private data
        can NEVER leak into the public server channel, even if delivery
        fails. The bot's own public confirmation text is intentionally
        NOT trusted for this (see DM_INSTRUCTIONS) — this function
        always gets the true outcome from Discord itself and asks
        ai_notice for an accurate line to post.
      - Only the FIRST match is treated as the DM payload (Groq is told
        max one block per reply); DM_TAG_PATTERN's DOTALL flag means a
        stray second [DM_START] would otherwise get greedily folded in,
        so .sub() below removes every match but only group(1) of the
        first is ever actually sent.
      - discord.Forbidden (DMs closed / "Allow DMs from server members"
        off) is caught explicitly and turned into an AI-phrased public
        heads-up instead of a silent failure or an unhandled-exception
        crash.

    Only meaningful on the GUILD reply path — get_ai_response only ever
    offers the tag (build_dm_directives) when guild is truthy, so a DM
    channel should never actually contain one. This function makes no
    assumption about that either way and just no-ops correctly if
    called with no tag present.
    """
    match = DM_TAG_PATTERN.search(server_content)
    if not match:
        return server_content

    dm_content = match.group(1).strip()

    # 🌸 Strip the ENTIRE tagged block (all matches, tags included) from
    # what goes to the server channel FIRST — before attempting delivery
    # at all — so a delivery failure below can never leave the private
    # payload sitting in text that gets sent publicly.
    public_content = DM_TAG_PATTERN.sub("", server_content).strip()

    if not dm_content:
        # 🌸 Empty tag body (model emitted [DM_START][DM_END] with
        # nothing inside) — nothing to DM, just fall through with the
        # already-cleaned public text. No notice needed either; nothing
        # actually happened.
        return public_content

    user = message.author

    try:
        await user.send(dm_content[:2000])
        print(f"✅ Sent DM split-content to {user.name} ({user.id})")
        notice = await ai_notice(message, "sent")
    except discord.Forbidden:
        # 🌸 PRIVACY GATEWAY — the user has "Allow direct messages from
        # server members" (or from this bot specifically) turned off.
        # Don't crash and don't silently drop it: get an AI-phrased,
        # in-character heads-up so they know to check that setting.
        print(f"⚠️ DM delivery blocked (Forbidden) for {user.name} ({user.id}) — privacy settings")
        notice = await ai_notice(message, "forbidden")
    except discord.HTTPException as e:
        # 🌸 Any other Discord-side delivery failure (rate limit, API
        # hiccup, etc.) — log it, still tell the user something went
        # wrong via the same "forbidden"-shaped notice rather than
        # silently returning nothing.
        print(f"⚠️ DM delivery failed for {user.name} ({user.id}): {e}")
        notice = await ai_notice(message, "forbidden")

    if notice:
        public_content = f"{public_content}\n{notice}".strip() if public_content else notice

    return public_content
