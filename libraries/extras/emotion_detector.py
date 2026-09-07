"""
🌸 EMOTION DETECTION — reads the vibe of a user's message so the bot
can match tone (gentle when someone's down, hype when they're hype,
normal otherwise), and — separately and more importantly — catches
crisis-level language so the bot NEVER stays in meme/slang mode when
someone might be in real danger.

Two totally separate systems on purpose:

1. classify_mood()      — casual, AI-based, "what's the vibe" for
                           everyday tone matching. Cheap Groq call.
                           Never trust this for safety decisions —
                           it's for texture, not triage.

2. check_crisis_signals() — dumb, fast, regex-based, fail-open-to-
                           caution keyword check for self-harm /
                           suicide / acute crisis language. Runs
                           BEFORE the personality/slang layer and,
                           if it fires, skips the AI entirely so
                           there's no chance a model call drops it,
                           gets classified wrong, or gets "in
                           character" about something that isn't a
                           joke.

Nothing here labels, diagnoses, or stores anything about the user.
It only decides "what tone should this one reply take."
"""

import re
import contextlib
import asyncio
from dataclasses import dataclass
from enum import Enum


class Mood(str, Enum):
    NEUTRAL = "neutral"
    UPSET = "upset"          # sad, frustrated, venting
    STRESSED = "stressed"    # anxious, overwhelmed
    EXCITED = "excited"      # hype, happy, celebratory
    ANGRY = "angry"          # mad at something/someone
    CRISIS = "crisis"        # see check_crisis_signals — highest priority


@dataclass
class EmotionResult:
    mood: Mood
    is_crisis: bool
    confidence: float  # 0.0-1.0, only meaningful for the AI path


# ─────────────────────────────────────────────────────────────────
# 🌸 LAYER 1: crisis keyword net — regex, not AI, on purpose.
# This must never depend on a model call succeeding, staying in
# budget, or classifying correctly under slang/typos. Keep this
# list conservative (catches real signals) rather than clever.
# ─────────────────────────────────────────────────────────────────

_CRISIS_PATTERNS = [
    r"\bkill (myself|me)\b",
    r"\b(want|wanna|going) to die\b",
    r"\bend (my|it all)\b.{0,15}\b(life|tonight|today)?\b",
    r"\bsuicid(e|al)\b",
    r"\bself[\s-]?harm\b",
    r"\bcutting myself\b",
    r"\bno reason to (live|be here)\b",
    r"\bbetter off (dead|without me)\b",
    r"\bcan'?t (go on|do this anymore|take it anymore)\b",
    r"\bdon'?t want to (be alive|exist|wake up)\b",
    r"\bi'?m going to (hurt|kill) (myself|me)\b",
]
_CRISIS_RE = re.compile("|".join(_CRISIS_PATTERNS), re.IGNORECASE)

# Resources kept short — the bot should say this plainly, once, and
# then keep being a supportive presence rather than a wall of links.
CRISIS_RESPONSE_NOTE = (
    "the user's message may indicate they're in crisis or having thoughts "
    "of self-harm/suicide. drop ALL slang/bot-persona/jokes for this reply. "
    "respond with genuine warmth and care, like a real friend would. "
    "gently encourage them to reach out to a crisis line or someone they "
    "trust — for the US: 988 (call or text), for other countries mention "
    "they can search '[their country] suicide crisis helpline'. don't "
    "diagnose them, don't lecture, don't make it weird — just be steady "
    "and kind and take it seriously. do not go back to being cute/funny "
    "until they've moved off this topic themselves."
)


def check_crisis_signals(text: str) -> bool:
    """
    🌸 Fast, dependency-free check for acute crisis language.
    Call this FIRST, before any AI classification or persona
    injection. If True, short-circuit straight to a caring,
    non-bot-persona reply — never mediate this through slang mode,
    the safeguard-decline system, or classify_mood().
    """
    if not text:
        return False
    return bool(_CRISIS_RE.search(text))


# ─────────────────────────────────────────────────────────────────
# 🌸 LAYER 2: casual mood classification — AI-based, for tone
# matching only (e.g. don't clown around if someone's venting about
# a bad day; match energy if someone's hyped). This is texture, not
# a clinical read. Reuses the existing cheap-classifier pattern
# already used elsewhere in the pipeline (low reasoning effort,
# small max_tokens, fails open to NEUTRAL on any error).
# ─────────────────────────────────────────────────────────────────

_MOOD_CLASSIFIER_SYSTEM_PROMPT = (
    "You read one Discord message and output ONLY one word describing "
    "the sender's apparent mood, nothing else, no punctuation, no "
    "explanation. Pick exactly one of: neutral, upset, stressed, "
    "excited, angry. Use 'neutral' whenever it's ambiguous, jokey, or "
    "just informational — don't over-read emotion into casual texting. "
    "Only pick upset/stressed/angry/excited when the mood is fairly "
    "clearly expressed."
)


async def classify_mood(groq_client, text: str) -> EmotionResult:
    """
    🌸 Cheap AI call to read casual emotional tone for reply-matching.
    NOT for safety decisions — check_crisis_signals() handles that
    separately and takes priority. Fails open to NEUTRAL so a
    classifier hiccup never blocks a normal reply.

    `groq_client` is expected to expose the same chat-completion
    interface used elsewhere in groq_ai.py / groq_instruct.py — pass
    whatever client object those modules already construct.

    NOTE: this bot's groq_client is a SYNC client (groq.Groq, not
    groq.AsyncGroq) — .chat.completions.create() returns a
    ChatCompletion directly, it's not awaitable. Running it straight
    with `await` raises "'ChatCompletion' object can't be awaited".
    So the blocking call is pushed onto a thread pool executor via
    run_in_executor, keeping this function's own `async def`/await
    contract for callers while not blocking the event loop on the
    actual network request.
    """
    if check_crisis_signals(text):
        # Belt-and-suspenders: even if something calls classify_mood
        # directly without checking crisis first, never let this path
        # under-call it. Crisis always wins.
        return EmotionResult(mood=Mood.CRISIS, is_crisis=True, confidence=1.0)

    if not text or not text.strip():
        return EmotionResult(mood=Mood.NEUTRAL, is_crisis=False, confidence=0.0)

    try:
        loop = asyncio.get_running_loop()

        def _call():
            return groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[
                    {"role": "system", "content": _MOOD_CLASSIFIER_SYSTEM_PROMPT},
                    {"role": "user", "content": text[:500]},  # cap input, this is cheap texture only
                ],
                max_tokens=100,
                reasoning_effort="low",
                temperature=0,
            )

        response = await loop.run_in_executor(None, _call)
        raw = (response.choices[0].message.content or "").strip().lower()
        for mood in Mood:
            if mood.value in raw and mood != Mood.CRISIS:
                return EmotionResult(mood=mood, is_crisis=False, confidence=0.8)
        return EmotionResult(mood=Mood.NEUTRAL, is_crisis=False, confidence=0.5)
    except Exception as e:
        print(f"⚠️ Error classifying mood: {e}")
        return EmotionResult(mood=Mood.NEUTRAL, is_crisis=False, confidence=0.0)


# ─────────────────────────────────────────────────────────────────
# 🌸 Tone hints fed into the personality prompt per mood. Kept light
# — nudges, not overrides, so the bot still sounds like itself.
# ─────────────────────────────────────────────────────────────────

_MOOD_TONE_HINTS = {
    Mood.UPSET: (
        "the user seems upset/down right now. dial back the jokes and "
        "slang for this reply, be genuinely warm and validating first, "
        "you can still be yourself but lead with actually caring."
    ),
    Mood.STRESSED: (
        "the user seems stressed/anxious. keep this reply calmer and "
        "more grounded than usual, less chaotic energy, help them feel "
        "steadier rather than adding to the noise."
    ),
    Mood.ANGRY: (
        "the user seems frustrated/angry, possibly not at you. don't "
        "match aggression back at them or get defensive, stay chill "
        "and let them vent, light humor only if they lead with it."
    ),
    Mood.EXCITED: (
        "the user's hyped/happy right now, feel free to match that "
        "energy and be excited with them."
    ),
    Mood.NEUTRAL: "",
}


def get_tone_hint(result: EmotionResult) -> str:
    """
    🌸 Returns a short instruction string to append to the personality
    prompt for this one reply, based on detected mood. Empty string
    for neutral/no-op so normal replies are completely unaffected.
    Crisis is intentionally NOT handled here — see check_crisis_signals
    and CRISIS_RESPONSE_NOTE, which should short-circuit the whole
    persona path rather than just nudging it.
    """
    if result.is_crisis:
        return CRISIS_RESPONSE_NOTE
    return _MOOD_TONE_HINTS.get(result.mood, "")


# ─────────────────────────────────────────────────────────────────
# 🌸 LAYER 3: auto-reaction — bot drops an emoji reaction onto the
# user's ORIGINAL message based on detected mood, on top of whatever
# text reply it sends. Purely cosmetic/expressive, so kept separate
# from the tone-hint logic above — a mood can change the reply's
# tone AND get a reaction, independently.
#
# CRISIS is deliberately excluded — no emoji reaction for a message
# that may be a cry for help, that's not the right register. Handle
# crisis only through CRISIS_RESPONSE_NOTE's actual caring reply.
# ─────────────────────────────────────────────────────────────────

_MOOD_REACTIONS = {
    Mood.UPSET: "🥺",
    Mood.STRESSED: "🫂",
    Mood.ANGRY: "😤",
    Mood.EXCITED: "🎉",
    Mood.NEUTRAL: None,   # no reaction for neutral — don't react to every message
    Mood.CRISIS: None,    # never react with an emoji here, see note above
}


def get_mood_reaction(result: EmotionResult) -> str | None:
    """
    🌸 Returns the emoji to react with for this detected mood, or
    None if no reaction should be added (neutral / crisis / unknown).
    """
    return _MOOD_REACTIONS.get(result.mood)


async def apply_mood_reaction(message, result: EmotionResult) -> None:
    """
    🌸 Adds the mood emoji reaction to the user's original discord
    message, if one applies. Call this alongside (not instead of)
    the normal AI reply — this is an addition, not a replacement.

    `message` is the discord.Message the user sent. Safe to call
    unconditionally after classify_mood() — it no-ops on
    neutral/crisis and swallows permission/rate-limit errors so a
    reaction failure never breaks the actual reply.
    """
    emoji = get_mood_reaction(result)
    if not emoji:
        return
    with contextlib.suppress(Exception):
        await message.add_reaction(emoji)


# ─────────────────────────────────────────────────────────────────
# 🌸 Async context manager — wraps "react while this mood is being
# handled, then clean up" into one `async with` block instead of
# manual add/remove pairs scattered across call sites. Useful if
# groq_service.py ever wants a temporary "seen" reaction (e.g. 👀)
# while the AI reply is generating, auto-swapped for the real mood
# emoji (or removed entirely) once the block exits — all error
# handling (missing perms, message deleted mid-flight, rate limits)
# is swallowed via contextlib.suppress so this never breaks the
# actual reply path either.
#
# Usage:
#     async with mood_reaction_scope(message, emotion_result):
#         response = await self.bot.groq.get_ai_response(...)
#         await message.reply(response)
#
# On enter: adds the mood emoji (same as apply_mood_reaction).
# On exit: no-op by default (the emoji just stays) — pass
# remove_on_exit=True if a call site wants the reaction cleared once
# the reply's been sent instead of left on the message.
# ─────────────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def mood_reaction_scope(message, result: EmotionResult, *, remove_on_exit: bool = False):
    """
    🌸 Async context manager version of apply_mood_reaction. Adds the
    mood emoji on entry; optionally removes it again on exit if
    remove_on_exit=True. No-ops entirely for neutral/crisis (nothing
    added, nothing to remove), same as apply_mood_reaction — crisis
    messages never get an emoji reaction regardless of how this is
    called.
    """
    emoji = get_mood_reaction(result)
    if not emoji:
        yield
        return

    with contextlib.suppress(Exception):
        await message.add_reaction(emoji)
    try:
        yield
    finally:
        if remove_on_exit:
            with contextlib.suppress(Exception):
                await message.remove_reaction(emoji, message.guild.me if message.guild else message.channel.me)
