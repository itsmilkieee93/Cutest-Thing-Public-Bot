"""🌸 Safety module — extracted from groq_ai.py's monolithic
GroqService. Structure-only split: same policies, same fail-open
contracts, same decline-generation logic as before.

check_safety / check_output_safety / _generate_safeguard_decline are
defined here as plain async functions taking `service` explicitly, so
they can reach service.client / service.async_client / service.bot /
service.default_model. service.py wires these onto GroqService as
bound methods.

PRESERVED CONTRACTS:
  - check_safety / check_output_safety FAIL OPEN on empty/error.
  - check_output_safety retries once on empty content before failing
    open.
  - Decline generation: random DECLINE_STYLES, personality from
    personality.py, banned phrases, flagged_text truncation, literal
    fallback "aw, can't help with that one 🥺💦".
"""
import random

from groq_instruct import (
    SAFEGUARD_MODEL, SAFEGUARD_POLICY, OUTPUT_SAFEGUARD_POLICY,
    _strip_reasoning, _check_base64_for_severe_terms, _contains_severe_term,
)
from personality import get_personality_for_nickname, load_personality


async def check_safety(service, prompt: str, username: str) -> bool:
    """
    🌸 Runs the incoming prompt through openai/gpt-oss-safeguard-20b
    (Groq's policy-based safety classifier) BEFORE it ever reaches the
    main chat model. Returns True if the message is safe to respond to,
    False if it should be blocked.

    FAILS OPEN: if the safeguard call itself errors out (network blip,
    rate limit, bad parse, etc.) this returns True so a Groq hiccup
    never silently mutes the bot for everyone — it only blocks on an
    actual UNSAFE verdict.

    Costs one extra Groq request per priority reply (doubles API usage
    against the free-tier limits) — worth knowing given past token-burn
    issues, so keep an eye on rate-limit logs after enabling this.
    """
    # 🌸 Layer 0: Check for base64-encoded harmful content (e.g.,
    # 'bmlnZ2E=' → 'nigga'). Catches encoding tricks before even
    # hitting the safeguard model.
    is_b64_flagged, decoded_b64 = _check_base64_for_severe_terms(prompt)
    if is_b64_flagged:
        print(f"🛡️ Base64 decoder caught harmful content from {username}: {decoded_b64!r} (was {prompt!r})")
        return False

    if not service.client:
        return True

    async def _call():
        return await service.async_client.chat.completions.create(
            model=SAFEGUARD_MODEL,
            messages=[
                {"role": "system", "content": SAFEGUARD_POLICY},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            # 🌸 gpt-oss-safeguard-20b is documented (see groq_instruct.py)
            # as a "safety REASONING model" — it spends tokens on hidden
            # chain-of-thought before writing SAFE/UNSAFE, so max_tokens=10
            # with no reasoning_effort almost certainly burned the whole
            # budget on invisible reasoning and returned empty content
            # every time. That's the WORST version of this bug found so
            # far: check_safety fails OPEN (returns True) on empty/failed
            # content, meaning this safety gate has likely been silently
            # rubber-stamping everything as safe instead of actually
            # classifying it. Same root cause as the classifier bugs
            # already fixed in groq_pexels.py, groq_exa_search.py, and
            # classify_server_query/classify_search_intent above —
            # reasoning_effort="low" + real max_tokens headroom fixes it.
            reasoning_effort="low",
            max_tokens=1024,
        )

    try:
        response = await _call()
        verdict = (response.choices[0].message.content or "").strip().upper()
        if not verdict:
            print(f"🛡️⚠️ Safeguard returned EMPTY content for {username} — check reasoning_effort/max_tokens (failing open)")
            return True
        is_safe = not verdict.startswith("UNSAFE")
        if not is_safe:
            print(f"🛡️ Safeguard blocked a message from {username}: {prompt[:80]!r}")
        return is_safe
    except Exception as e:
        print(f"⚠️ Safeguard check error (failing open): {e}")
        return True


async def check_output_safety(service, reply: str, username: str) -> bool:
    """
    🌸 Runs the CHAT MODEL'S OWN REPLY through the safeguard model,
    catching cases where an innocent-looking prompt (e.g. a cipher/
    decode request) tricked the model into generating disallowed
    content that check_safety's prompt-only screening would never see.

    Returns True if the reply is safe to send, False if it should be
    replaced with a refusal.

    Three layers, cheapest first:
      1. Instant local regex/normalization check for severe terms —
         catches slurs even through leetspeak/spacing obfuscation with
         zero extra latency or API cost.
      2. Base64 decoder check — catches if reply contains encoded
         harmful content (e.g., a model tricked into generating 'bmlnZ2E=').
      3. Groq safeguard model call for everything else — same
         fail-open behavior as check_safety (a Groq hiccup here should
         never mean the bot goes silent), but layers 1-2 already cover
         the worst case even if this call fails.
    """
    # Layer 1: Direct severe term check
    if _contains_severe_term(reply):
        print(f"🛡️ Local filter blocked a reply meant for {username}: {reply[:80]!r}")
        return False

    # Layer 2: Base64-encoded content check
    is_b64_flagged, decoded_b64 = _check_base64_for_severe_terms(reply)
    if is_b64_flagged:
        print(f"🛡️ Base64 check caught harmful output from model for {username}: {decoded_b64!r}")
        return False

    if not service.client:
        return True

    # 🌸 Defensive truncation — this is the one safeguard call that
    # screens the CHAT MODEL'S full reply (not a short user prompt), so
    # an unusually long reply can eat into the reasoning/output budget
    # and starve the verdict token even with max_tokens headroom below.
    # Slurs/harmful content that would trigger UNSAFE virtually always
    # show up early; capping the input here doesn't weaken the check.
    reply_for_check = reply[:2000]

    async def _call():
        return await service.async_client.chat.completions.create(
            model=SAFEGUARD_MODEL,
            messages=[
                {"role": "system", "content": OUTPUT_SAFEGUARD_POLICY},
                {"role": "user", "content": reply_for_check},
            ],
            temperature=0,
            # 🌸 Same reasoning-model fix as check_safety above.
            reasoning_effort="low",
            max_tokens=1024,
        )

    try:
        response = await _call()
        verdict = (response.choices[0].message.content or "").strip().upper()

        # 🌸 One retry before failing open — this gate gets a single
        # transient empty-content response more often than it should,
        # and failing open on the FIRST miss defeats the point of an
        # output safety check. A second attempt costs one extra call
        # only in the rare empty case, not on the normal path.
        if not verdict:
            print(f"🛡️⚠️ Output safeguard returned EMPTY content for {username} on first attempt — retrying once")
            response = await _call()
            verdict = (response.choices[0].message.content or "").strip().upper()

        if not verdict:
            print(f"🛡️⚠️ Output safeguard returned EMPTY content for {username} on retry too — check reasoning_effort/max_tokens (failing open)")
            return True
        is_safe = not verdict.startswith("UNSAFE")
        if not is_safe:
            print(f"🛡️ Output safeguard blocked a reply meant for {username}: {reply[:80]!r}")
        return is_safe
    except Exception as e:
        print(f"⚠️ Output safeguard check error (failing open): {e}")
        return True


async def _generate_safeguard_decline(service, username: str, display_name: str = None, guild=None, flagged_text: str = None) -> str:
    """
    🌸 LIVE decline, not a template. Previously both the input-side
    (check_safety) and output-side (check_output_safety) blocks fell
    back to `random.choice(SAFEGUARD_BLOCK_REPLIES)` — the same 4
    canned lines on repeat forever, which reads as an obvious
    copy-pasted bot response instead of the bot actually "talking".
    This makes one small, cheap Groq call so the decline is generated
    fresh every time, in the bot's real personality, addressed to the
    actual person — same idea as every other reply, just short and
    firm. Kept deliberately separate from get_ai_response (no memory
    load/save, no search, no history) so a blocked message costs as
    little as possible while still sounding alive.

    🌸 Now pulls the bot's CURRENT per-guild personality/nickname from
    personality.py (same source get_ai_response's main system prompt
    uses) instead of a separate hardcoded "cute gen-z bot" blurb, so a
    decline matches whatever nickname/vibe /server-persona-set gave
    that guild instead of sounding like a generic stand-in bot.

    🌸 System prompt is JUST personality_instructions — no extra
    decline-specific PERSONALITY rules layered on top of it, so the
    bot's core voice/vibe still comes entirely from personality.py.
    (Update: the user turn below DOES now carry decline-specific
    constraints — style + banned phrases — but those are call-
    structure/variety constraints, not personality rules, and they
    live in the user turn, not the system prompt, for exactly that
    reason: they shape HOW this one reply is built, they don't
    redefine WHO the bot is.)

    🌸 No more static SAFEGUARD_BLOCK_REPLIES template — the decline
    is ALWAYS AI-generated now. One retry on empty content before
    giving up (mirrors check_output_safety's retry), and only a
    single bare literal string is left as the true last-resort (no
    client configured, or both generation attempts failed/errored) so
    the bot never goes silent — that string is a safety-net, not a
    rotating template.

    🌸 `flagged_text` — the actual message that got blocked (either
    the user's prompt, for an input-side block, or the model's own
    would-be reply, for an output-side block). Previously the user
    turn here was a content-free stub ("sent something you're
    declining to engage with") — with literally nothing to react to,
    the model kept converging on the same generic brush-off ("not
    feeling that one 😅") every single call, which is exactly the
    repetitive-template look this function was meant to avoid.
    Feeding it the real flagged text gives the model something
    concrete to riff on in-character, so declines actually vary like
    every other reply does. Truncated hard — this is a stub the
    model reacts to, not something it should quote back at length,
    and it keeps the call cheap.

    🌸 personality.py's "no corporate refusals" rule (see
    PERSONALITY_TEMPLATE) only tells the model what NOT to say — it
    doesn't push for variety, so the model's safest/highest-probability
    "casual no" kept landing on the same phrasing every time, which is
    just as repetitive as the corporate refusal it was avoiding. The
    explicit "vary how you say no" nudge below lives here rather than
    in personality.py because it's specific to this narrow decline
    path, not something every normal reply needs reminding of.

    🌸 Turns out "vary how you say no" alone wasn't enough — the model
    (openai/gpt-oss-120b) has no memory of what it said on the LAST
    decline, so an instruction to "not repeat itself" is something it
    literally can't act on; it can only pick whatever's highest-
    probability for THIS call, which converges on the same shape
    ("Nah, I'm gonna pass on that 🌚" / "Nah, I'm not gonna help with
    that 🚫") even at temperature=1.05. Fix: pick a random OPENING
    STYLE (not full sentence — just a shape/angle) in code and hand
    THAT to the model as a hard constraint, so the variety comes from
    an explicit dice roll on our side instead of hoping high-temp
    sampling breaks the model's own habit.

    🌸 Even with varied styles, a few of them (curt/unbothered) kept
    landing on bare "No." / "nope :P" with zero reason — technically
    varied, but unhelpful for anyone reading it (including mods
    trying to understand what got flagged). DECLINE_STYLES now all
    point toward including the real reason, and the user turn makes
    it a hard requirement regardless of which style got picked: style
    controls the TONE, this controls the CONTENT — a decline can be
    short and blunt and still name the reason in the same breath.
    """
    DECLINE_STYLES = [
        "deflect with a joke or a random tangent, but still slip in the real reason you won't do it — let some genuine feeling (a wince, a laugh, mock offense) come through",
        "act genuinely confused/taken aback why they'd even ask, then explain what's off about the request — react like it actually caught you off guard",
        "call out the request itself (e.g. 'that's a wild one to ask a bot lol') and say why it's a no — a little dramatic/exasperated is good here",
        "say no plainly but warmly, then give a short reason right after — no lecture, just real feeling behind the reason",
        "turn it back on them with a question first, then land on the reason you're not doing it — curious/teasing energy",
        "act unbothered/bored by the request, but still toss out the reason almost as an afterthought — dry, not cold",
    ]
    decline_style = random.choice(DECLINE_STYLES)

    # 🌸 True last-resort only (no client, or both generation attempts
    # failed/errored) — deliberately has NO reason attached, since at
    # this point we have no live model call to generate one from and
    # a fake/generic reason would be worse than none.
    FALLBACK_DECLINE = "aw, can't help with that one 🥺💦"

    if not service.client:
        return FALLBACK_DECLINE

    # 🌸 Resolve live personality instructions the same way the main
    # chat path does: per-guild nickname lookup if we have a guild,
    # else the global default template (e.g. DMs, or lookup failure).
    try:
        if guild is not None and service.bot is not None:
            personality_instructions = await load_personality(service.bot, guild.id)
        else:
            personality_instructions = get_personality_for_nickname(None)
    except Exception as e:
        print(f"⚠️ Safeguard decline personality load failed (using default): {e}")
        personality_instructions = get_personality_for_nickname(None)

    async def _call():
        return await service.async_client.chat.completions.create(
            model=service.default_model,
            messages=[
                {
                    "role": "system",
                    "content": personality_instructions,
                },
                {
                    "role": "user",
                    "content": (
                        f"[{display_name or username} (@{username}) sent this, and "
                        f"you're declining to engage with it: {flagged_text[:200]!r}. "
                        f"Reply in character, reacting to what it actually was. "
                        f"For THIS reply specifically: {decline_style}. "
                        "No matter the style, the reply MUST include why you're "
                        "saying no — a short, casual, real reason, not a vague "
                        "brush-off. A bare 'No.' or 'nope :P' with no reason at "
                        "all is not acceptable here, even if the style above "
                        "leans short or unbothered — keep it brief but the reason "
                        "has to be there. "
                        "Make it feel EMOTIONAL, not flat, use Gen Z language slang — let real feeling show "
                        "(surprised, teasing, a little dramatic, whatever fits the "
                        "style above) instead of a neutral/robotic no. Include at "
                        "least 1-2 emoji that match that feeling. "
                        "Don't say 'I'm not gonna help with that' or 'gonna pass "
                        "on that' or any close variant — those are banned for "
                        "this reply, find a different way in.]"
                        if flagged_text else
                        f"[{display_name or username} (@{username}) sent something "
                        "you're declining to engage with. Reply in character. "
                        f"For THIS reply specifically: {decline_style}. "
                        "No matter the style, the reply MUST include why you're "
                        "saying no — a short, casual, real reason, not a vague "
                        "brush-off. "
                        "Make it feel EMOTIONAL, not flat, use Gen Z language slang — let real feeling show "
                        "instead of a neutral/robotic no. Include at least 1-2 "
                        "emoji that match that feeling. "
                        "Don't say 'I'm not gonna help with that' or 'gonna pass "
                        "on that' or any close variant.]"
                    ),
                },
            ],
            temperature=1.05,
            reasoning_effort="low",
            max_tokens=768,
        )

    try:
        response = await _call()
        decline = _strip_reasoning(response.choices[0].message.content or "").strip()

        if not decline:
            print(f"🛡️⚠️ Safeguard decline generation returned EMPTY for {username} on first attempt — retrying once")
            response = await _call()
            decline = _strip_reasoning(response.choices[0].message.content or "").strip()

        if not decline:
            print(f"🛡️⚠️ Safeguard decline generation returned EMPTY for {username} on retry too — using literal fallback")
            return FALLBACK_DECLINE
        return decline
    except Exception as e:
        print(f"⚠️ Safeguard decline generation failed (using literal fallback): {e}")
        return FALLBACK_DECLINE
