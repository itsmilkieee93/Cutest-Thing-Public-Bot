"""🌸 DM-notice module — extracted from groq_ai.py's monolithic
GroqService. Kept together per the refactor spec ("Keep
_first_notice_line with generate_dm_notice"). Structure-only split —
same CLASSIFIER_MODEL, same outcome handling, same static fallbacks,
same finish_reason=="length" guard as before.

generate_dm_notice is defined here as a plain async function taking
`service` explicitly (needs service.bot / service.client /
service.async_client), wired onto GroqService as a bound method by
service.py.
"""
import re
import discord

from groq_instruct import CLASSIFIER_MODEL, _strip_reasoning
from personality import get_personality_for_nickname, load_personality
from extras.groq_dm_instruct import format_snowflake_info  # noqa: F401  (re-exported for compatibility)


def _first_notice_line(text: str, must_contain: str | None = None) -> str:
    """🌸 Defensive backstop for generate_dm_notice — collapses a
    completion down to just ONE paragraph, even if the model ignored
    the "exactly one sentence" instruction and tacked on a second,
    unrelated greeting/thought (the actual bug seen in production: two
    full sentences stacked in one reply, e.g. a DM confirmation
    immediately followed by an unrelated "hope you're vibing!" line).

    Splits on blank lines first (paragraph-level — the observed failure
    mode was always two DOUBLE-newline-separated sentences, i.e. the
    model treating them as two separate "messages"), then falls back to
    single newlines if there's no blank-line split at all.

    If `must_contain` is given (the Forbidden-outcome mention), prefers
    whichever paragraph actually contains it — so truncating never
    accidentally throws away the one line with the required @-mention
    in favor of an earlier, mention-less paragraph.
    """
    text = text.strip()
    if not text:
        return text

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) <= 1:
        # 🌸 No blank-line split — try single newlines as a weaker signal
        # of "the model stacked separate thoughts on separate lines".
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if len(paragraphs) <= 1:
        return text

    if must_contain:
        for p in paragraphs:
            if must_contain in p:
                return p

    return paragraphs[0]


async def generate_dm_notice(service, message: discord.Message, outcome: str) -> str:
    """
    🌸 Generates the PUBLIC line reporting what actually happened
    with a DM split-route delivery — see extras/groq_dm_instruct.py's
    route_dm_split, which calls this as its `ai_notice` callback.

    outcome is "sent" (user.send succeeded) or "forbidden" (DMs
    closed / delivery failed) — the REAL Discord-verified result,
    never the model's own guess. This exists specifically so the
    public message never lies about delivery status the way the
    model's own inline confirmation could (it doesn't know yet
    whether the DM will land when it writes that text) — same
    "small, cheap, in-character, no memory load" call as
    _generate_safeguard_decline, just reporting a different fact.

    Uses CLASSIFIER_MODEL (openai/gpt-oss-20b), NOT service.default_model
    — this is a short, templated, low-stakes fill-in-the-blank line,
    not a full conversational reply, so the smaller/cheaper/faster
    model is the right fit (same reasoning as classify_server_query's
    use of CLASSIFIER_MODEL elsewhere in this file). Being a REASONING
    model, it spends part of its token budget on hidden chain-of-
    thought before any visible content — see notice_max_tokens below
    for why the caps are set higher than the visible sentence alone
    would need.

    FAILS to a short static line per outcome if the client is
    missing or the call errors/comes back empty — reporting SOME
    accurate status always beats reporting nothing, and reporting
    nothing would look identical to the feature silently doing
    nothing at all.
    """
    static_fallback = {
        "sent": "sent it to your DMs! 🌸💌",
        "forbidden": (
            f"{message.author.mention} I tried to DM you that but your "
            f"privacy settings are blocking me! 🌸 Check **Privacy "
            f"Settings > \"Allow direct messages from server members\"** "
            f"for this server and I'll try again~"
        ),
    }.get(outcome, "")

    if not service.client:
        return static_fallback

    try:
        if message.guild is not None:
            personality_instructions = await load_personality(service.bot, message.guild.id)
        else:
            personality_instructions = get_personality_for_nickname(None)
    except Exception as e:
        print(f"⚠️ DM notice personality load failed (using default): {e}")
        personality_instructions = get_personality_for_nickname(None)

    display_name = message.author.display_name
    username = message.author.name

    if outcome == "sent":
        scene = (
            f"[You just successfully sent {display_name} (@{username}) a "
            "DM with something they asked for / something private. Tell "
            "them, in the SERVER channel, in character, that it's in "
            "their DMs now. EXACTLY ONE short sentence — no greeting, no "
            "second sentence, no extra thought tacked on after it. Just "
            "the one confirmation line and stop.]"
        )
        # 🌸 "sent" only ever needs a short single confirmation
        # ("sent it to your DMs! 🌸💌"). CLASSIFIER_MODEL is a
        # REASONING model (see classify_server_query's _call above) —
        # it spends tokens on hidden chain-of-thought before any
        # visible content, so the budget has to cover BOTH a short
        # reasoning pass AND the sentence itself, not just the
        # sentence. 120 leaves real headroom for that.
        notice_max_tokens = 120
    else:
        scene = (
            f"[You just TRIED to DM {display_name} (@{username}) "
            "something, but Discord blocked it — their privacy settings "
            "don't allow DMs from server members. Tell them this, in "
            "character, in the SERVER channel, and that they should "
            "check their Privacy Settings ('Allow direct messages from "
            f"server members') for this server so you can try again. "
            f"You MUST @-mention them by writing exactly {message.author.mention} "
            "somewhere in your reply. EXACTLY ONE short sentence covering "
            "both the problem and the fix — no greeting, no second "
            "sentence, no extra thought tacked on after it.]"
        )
        # 🌸 "forbidden" needs a full mention + explanation + fix
        # instruction in one sentence (~45-55 tokens on its own — see
        # the max_tokens=40 cutoff bug this already fixed once) PLUS
        # the same reasoning-token overhead as above — 180 covers
        # both comfortably.
        notice_max_tokens = 256

    async def _call():
        return await service.async_client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[
                {"role": "system", "content": personality_instructions},
                {"role": "user", "content": scene},
            ],
            temperature=0.9,
            reasoning_effort="low",
            max_tokens=notice_max_tokens,
        )

    try:
        response = await _call()
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        notice = _strip_reasoning(response.choices[0].message.content or "").strip()
        if not notice:
            print(f"⚠️ DM notice generation returned EMPTY for {username} (outcome={outcome}) — using static fallback")
            return static_fallback

        # 🌸 HARD-TRUNCATION GUARD — if the completion got cut off by
        # hitting notice_max_tokens mid-sentence (finish_reason ==
        # "length"), the API stopped wherever the budget ran out, not
        # at a sentence boundary (the exact bug seen in production:
        # "...your privacy settings dis" cut off mid-word). Ship the
        # guaranteed-complete static line instead of a mangled one.
        if finish_reason == "length":
            print(f"⚠️ DM notice generation hit max_tokens (outcome={outcome}) — using static fallback to avoid mid-sentence cutoff")
            return static_fallback

        # 🌸 DEFENSIVE BACKSTOP — the scene prompt says "EXACTLY ONE
        # sentence", but low-effort/high-temp completions sometimes
        # still tack on a second unrelated greeting/thought anyway
        # (e.g. "sent it! 🌸" + "\n\nYo! Hope you're vibing! ✨"). Rather
        # than trust the model's restraint alone, collapse to just the
        # single paragraph/sentence that actually matters, so a stray
        # extra line can never reach the channel.
        notice = _first_notice_line(notice, must_contain=message.author.mention if outcome == "forbidden" else None)

        # 🌸 Forbidden notices MUST actually ping the user (so they
        # see it/know to check settings) — if the model dropped the
        # mention despite being told to include it (even after the
        # truncation above tried to keep the mention-bearing line),
        # fall back to the guaranteed-correct static line rather than
        # risk a silent, un-pinged notice nobody notices.
        if outcome == "forbidden" and message.author.mention not in notice:
            return static_fallback
        return notice
    except Exception as e:
        print(f"⚠️ DM notice generation failed for {username} (outcome={outcome}): {e}")
        return static_fallback
