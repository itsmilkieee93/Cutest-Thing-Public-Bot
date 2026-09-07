"""🌸 extras/groq_math.py

AI-classified math interceptor for the mention/reply pipeline.

Same two-stage shape as every other interceptor in this pipeline
(handle_media_request's AI classifier, _generate_safeguard_decline's
"compute deterministically, then let Groq phrase it" pattern):

  1. CLASSIFY — one cheap Groq call (smallest model, tiny output) decides
     whether this message is actually asking for a math computation, and
     if so extracts the clean expression. Zero-token local pre-filter
     first so casual chat never spends a Groq call on this at all.
  2. COMPUTE — calculator.py's proven safe-eval engine (_preprocess,
     _safe_eval, _format) — the SAME engine backing /math — evaluates the
     extracted expression. This is the part that must never hallucinate:
     Groq has no arithmetic guarantees, so the actual number always comes
     from this deterministic path, never from the classifier or the
     phrasing call.
  3. PHRASE — a second cheap Groq call takes the verified-correct result
     and phrases it naturally, in character, instead of a dry embed
     dump — same "AI narrates a fact a deterministic system already
     computed" shape as the snowflake decoder / server-info answers.
     Falls back to a plain but still cute sentence if this call fails,
     so a phrasing hiccup never blocks the (already-correct) answer from
     reaching the user.

Returns a plain string (not a discord.Embed) so it flows into the normal
message.reply() text path in handle_mention_reaction, same as a regular
Groq chat response — no more "this looks like a calculator receipt"
embed. Returns None if this isn't a math request at all, so it falls
through to the rest of the pipeline untouched.
"""

import re
import json

from calculator import _preprocess, _safe_eval, _format

# 🌸 Zero-token local pre-filter — same shape as server_hint/
# MUSIC_INTENT_PATTERN elsewhere in this pipeline. Cheap enough to run on
# EVERY message so the Groq classifier call below only fires for messages
# that have at least a fighting chance of being math. Deliberately loose
# (a plain digit or common math word is enough) since the actual judgment
# call — "is this REALLY asking me to compute something" — is Groq's job
# in classify_math_request, not this filter's. This just saves the
# classifier call on obviously-unrelated chat ("ur cute 🥺", "lol how are u").
_MATH_HINT_PATTERN = re.compile(
    r"[\d].*[+\-*/×÷^%]|[+\-*/×÷^%].*[\d]|"
    r"\b(calculate|solve|compute|sqrt|square root|factorial|"
    r"math|equation|percent|percentage)\b",
    re.IGNORECASE,
)


def _math_hint(text: str) -> bool:
    """🌸 Zero-token gate — mirrors server_hint's role in groq_service.py's
    dispatch: decides whether classify_math_request is even worth calling."""
    return bool(text and _MATH_HINT_PATTERN.search(text))


async def classify_math_request(message_text: str, groq_client) -> str | None:
    """🌸 ONE cheap Groq call (smallest model, tiny output) that decides:
      (a) is this actually a math computation request, and
      (b) if so, what's the clean expression to evaluate?

    Returns the extracted expression string (e.g. "9*8*9*9", "sqrt(144)")
    ready for calculator.py's _preprocess/_safe_eval, or None if this
    isn't a math request at all (casual mention of a number, a date, an
    ID, "how much is rent" with no actual arithmetic, etc).

    Mirrors groq_pexels.py's _classify_media_request shape: reasoning_effort
    "low" + a small max_tokens, JSON-only output, fail-open on any error/
    empty content/junk response so a classifier hiccup falls through to
    normal Groq chat instead of blocking the message.
    """
    try:
        response = await groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            reasoning_effort="low",
            max_tokens=150,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You classify whether a Discord message is asking for a "
                        "MATH COMPUTATION (arithmetic, algebra, trig, roots, "
                        "factorials, percentages, etc — anything with a "
                        "numeric answer that can be evaluated). "
                        "If yes, extract ONLY the clean mathematical expression "
                        "using Python syntax (* for multiply, ** for power, "
                        "sqrt(), sin(), pi, etc — matching Python's math module). "
                        "Ignore surrounding chatter like 'send it to my dm' or "
                        "'and tell me the result' — those are routing "
                        "instructions, not part of the expression. "
                        "If the message is NOT a math request (a date, an ID, "
                        "a casual mention of a number, a question with no "
                        "computable expression), respond with is_math=false. "
                        'Respond ONLY with JSON: {"is_math": true/false, '
                        '"expression": "..."} — no markdown, no preamble.'
                    ),
                },
                {"role": "user", "content": message_text},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            print("⚠️ classify_math_request: empty content from Groq — failing open")
            return None

        content = content.strip().removeprefix("```json").removesuffix("```").strip()
        data = json.loads(content)

        if not data.get("is_math"):
            return None

        expression = (data.get("expression") or "").strip()
        return expression or None

    except Exception as e:
        print(f"⚠️ classify_math_request failed, falling open: {e}")
        return None


async def phrase_math_result(
    groq_client, user_text: str, expression: str, result_str: str, display_name: str,
) -> str | None:
    """🌸 Second cheap Groq call — takes the ALREADY-COMPUTED, verified-
    correct result and phrases it naturally in the bot's kawaii voice,
    instead of a raw "Expression: ... Result: ..." embed dump. The
    NUMBER itself is never regenerated here — it's handed in as a fact
    Groq narrates, same shape as _generate_safeguard_decline phrasing a
    decision that was already made, or the snowflake decoder handing
    Groq an already-decoded date to describe. Groq cannot alter the
    answer, only the wording around it.

    Returns None on any failure so the caller can fall back to a plain
    (still correct, still cute) sentence rather than blocking the reply.
    """
    try:
        response = await groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            reasoning_effort="low",
            max_tokens=200,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Cutest Thing, a kawaii Discord bot 🌸✨. "
                        "The user asked a math question and it has ALREADY "
                        "been computed correctly by a calculator — your ONLY "
                        "job is to state that exact result naturally and "
                        "cutely in 1-2 short sentences. Do NOT recompute, "
                        "second-guess, or alter the number in any way — just "
                        "report it exactly as given, in your own warm voice. "
                        "No markdown headers, no code blocks, just a natural "
                        "chat reply."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{display_name} asked: {user_text}\n"
                        f"Expression: {expression}\n"
                        f"Correct result (do not change this number): {result_str}"
                    ),
                },
            ],
        )
        content = response.choices[0].message.content
        return content.strip() if content else None
    except Exception as e:
        print(f"⚠️ phrase_math_result failed, using plain fallback: {e}")
        return None


async def handle_math_request(message, guild_id: int, shared, groq_client) -> str | None:
    """🌸 Full three-stage math interceptor — see module docstring.
    Returns a natural-language string ready for message.reply(), or None
    if this doesn't look like (or isn't confirmed as) a math request, so
    the rest of the mention pipeline runs untouched.

    guild_id/shared are accepted for signature parity with the other
    interceptors in groq_service.py's dispatch chain (handle_media_request,
    handle_music_request) even though this one doesn't need DB access.

    groq_client is passed in explicitly by the caller (self.bot.groq.client)
    rather than pulled off `shared` — `shared` here is the resources.shared
    MODULE, not the bot instance, so it has no `.bot` attribute. Every other
    call site that needs the Groq client (e.g. describe_attachments in
    handle_mention_reaction) gets it the same explicit way.
    """
    raw = message.content.strip()
    raw = re.sub(r"^<@!?\d+>\s*", "", raw).strip()

    if not raw or not _math_hint(raw):
        return None

    expression = await classify_math_request(raw, groq_client)
    if not expression:
        return None

    expr = _preprocess(expression)

    try:
        result = _safe_eval(expr)
        result_str = _format(result)
    except ValueError as e:
        # 🌸 Confirmed math request but it errored (div by zero, bad
        # syntax the classifier still let through) — still worth a
        # natural in-character reply rather than silently falling
        # through to Groq to re-guess the same broken expression.
        return f"Aww, I can't solve that one — {e} 💦"
    except Exception:
        # 🌸 Anything unexpected — fail open, let normal Groq chat
        # handle it instead of surfacing a scary raw error.
        return None

    natural_reply = await phrase_math_result(
        groq_client, raw, expression, result_str, message.author.display_name,
    )
    if natural_reply:
        return natural_reply

    # 🌸 Plain fallback if the phrasing call itself failed — the ANSWER
    # is still guaranteed correct even if Groq couldn't dress it up.
    return f"{expression} = **{result_str}** 🧮✨"
