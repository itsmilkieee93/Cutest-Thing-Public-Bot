"""🌸 Classifier module — extracted from groq_ai.py's monolithic
GroqService. Structure-only split: same models, same prompts, same
fail-open contracts as before.

classify_server_query / classify_search_intent / classify_user_intent
are defined here as plain async functions taking `service` (the
GroqService instance) explicitly, so they can still reach
service.client / service.async_client. service.py wires these onto
GroqService as bound methods so external callers doing
`self.classify_server_query(...)` from within GroqService, or
`groq_service_instance.classify_server_query(...)` from outside,
keep working unchanged.

FAIL-OPEN CONTRACTS (unchanged):
  - classify_server_query → "none" on ANY problem (no client, API
    error, timeout, empty reply, or a label outside SERVER_QUERY_LABELS).
  - classify_search_intent → False on ANY problem, so the caller's
    _wants_web_search regex fallback in get_ai_response always runs.
  - classify_user_intent → "GENERAL_CHAT" on ANY problem (no client,
    rate limit, network blip, empty reply, or a label outside the pool).
"""
import re
import asyncio

from groq import RateLimitError as GroqRateLimitError

from groq_instruct import (
    CLASSIFIER_MODEL, SERVER_QUERY_LABELS, SERVER_QUERY_CLASSIFIER_POLICY,
    SEARCH_INTENT_CLASSIFIER_POLICY,
)


async def classify_server_query(service, message_content: str, username: str) -> str:
    """
    🌸 AI-FIRST server-query router. One cheap Groq call (smallest
    model in MODEL_POOL, max_tokens=6, temperature=0) decides which
    server-info handler this message wants, so paraphrases that don't
    match any hand-written regex still route correctly.

    Returns one of SERVER_QUERY_LABELS. Returns "none" on ANY
    problem — no client, API error, timeout, empty reply, or a label
    that isn't in SERVER_QUERY_LABELS — so the caller's regex chain
    always runs as the fallback. This call is intentionally
    best-effort: it should never be the reason a server-info question
    goes unanswered.
    """
    if not service.client:
        return "none"

    async def _call():
        return await service.async_client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[
                {"role": "system", "content": SERVER_QUERY_CLASSIFIER_POLICY},
                {"role": "user", "content": message_content},
            ],
            temperature=0,
            # 🌸 CLASSIFIER_MODEL (openai/gpt-oss-20b) is a REASONING
            # model — it spends tokens on hidden chain-of-thought
            # before emitting any visible content, so max_tokens=6
            # with no reasoning_effort left it burning the entire
            # budget on invisible reasoning before ever reaching the
            # actual label, coming back as empty content (which this
            # function's own except/fallback then silently absorbed
            # as "none" every time — same root cause already found
            # and fixed in groq_pexels.py and groq_exa_search.py).
            # reasoning_effort="low" + enough max_tokens for a short
            # reasoning pass + the label fixes it.
            reasoning_effort="low",
            max_tokens=150,
        )

    try:
        response = await _call()
        label = (response.choices[0].message.content or "").strip().lower()
        if not label:
            print(f"⚠️ Server-query classifier returned EMPTY content for {username} — check reasoning_effort/max_tokens")
            return "none"
        # 🌸 Defensive parse: strip stray punctuation/quotes the model
        # sometimes wraps a single-word answer in, then take just the
        # first token in case it still adds a trailing word.
        label = re.sub(r"[^a-z_]", " ", label).split()
        label = label[0] if label else ""
        if label not in SERVER_QUERY_LABELS:
            return "none"
        return label
    except Exception as e:
        print(f"⚠️ Server-query classifier error (falling back to regex) for {username}: {e}")
        return "none"


async def classify_search_intent(service, prompt: str, username: str) -> bool:
    """
    🌸 AI-FIRST search-intent router — same shape as
    classify_server_query above, just a YES/NO instead of a label.
    One cheap Groq call (smallest model in MODEL_POOL, max_tokens=4,
    temperature=0) decides whether `prompt` needs a live web search,
    so paraphrases the SEARCH_INTENT_PATTERN regex would miss (e.g.
    "did they release the sequel yet", "how's Bitcoin doing rn")
    still route to Exa correctly.

    Returns True only on a clean "YES". Returns False on ANY problem
    — no client, API error, timeout, empty reply, or anything that
    isn't recognizably yes/no — so the caller's _wants_web_search
    regex always runs as the fallback. This call is intentionally
    best-effort: it should never be the reason a search request goes
    unanswered, and it should never be the reason an ordinary chat
    reply gets accidentally routed to Exa/compound either.
    """
    if not service.client or not prompt:
        return False

    async def _call():
        return await service.async_client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[
                {"role": "system", "content": SEARCH_INTENT_CLASSIFIER_POLICY},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            # 🌸 Same reasoning-model fix as classify_server_query
            # above — max_tokens=4 left zero room for gpt-oss-20b's
            # hidden reasoning pass to finish before the budget ran
            # out, so this was almost certainly always returning
            # empty content and silently falling back to False/regex
            # every single call.
            reasoning_effort="low",
            max_tokens=150,
        )

    try:
        response = await _call()
        verdict = (response.choices[0].message.content or "").strip().upper()
        if not verdict:
            print(f"⚠️ Search-intent classifier returned EMPTY content for {username} — check reasoning_effort/max_tokens")
            return False
        return verdict.startswith("YES")
    except Exception as e:
        print(f"⚠️ Search-intent classifier error (falling back to regex) for {username}: {e}")
        return False


async def classify_user_intent(service, user_message: str) -> str:
    """
    🌸 INTENT CLASSIFIER — runs BEFORE the expensive main Groq chat
    generation, so a handful of common, cheaply-answerable intents
    (staff lookups, role lookups, math, media/music fetches) never
    have to pay for a full conversational completion at all. Same
    fail-open
    philosophy as classify_server_query/classify_search_intent
    elsewhere in this file, just a wider label pool covering
    several interceptors at once instead of one.

    Deliberately uses service.client (the SYNC Groq SDK), NOT
    service.async_client — every other classifier in this file already
    calls the async client natively, so this one exists specifically
    to demonstrate/own the "blocking call pushed into a worker
    thread" pattern: loop.run_in_executor(None, _call) keeps the
    blocking service.client.chat.completions.create(...) off the main
    event loop, the same way you'd have to if this SDK only shipped
    a sync client at all.

    Returns exactly one of: QUERY_STAFF, QUERY_ROLE, QUERY_MATH,
    FETCH_MEDIA, FETCH_MUSIC, GENERAL_CHAT. Returns "GENERAL_CHAT"
    (fail-open) on ANY problem — no client, rate limit, network
    blip, empty reply, or a label outside the pool — so a
    classifier hiccup only ever costs one extra full chat turn,
    never a dropped/broken reply.
    """
    if not service.client or not user_message:
        return "GENERAL_CHAT"

    labels = ("QUERY_STAFF", "QUERY_ROLE", "QUERY_MATH", "FETCH_MEDIA", "FETCH_MUSIC", "GENERAL_CHAT")

    system_prompt = (
        "You are an intent classifier for a Discord bot. Read the user's "
        "message and output EXACTLY ONE label, in uppercase, with nothing "
        "else — no punctuation, no explanation, no reasoning shown.\n\n"
        "Labels:\n"
        "QUERY_STAFF — asking who the staff/mods/moderators are\n"
        "QUERY_ROLE — asking about roles in general: who holds a SPECIFIC "
        "named role (other than staff/mod), a rank/color-name role list, "
        "permissions a role has, or what roles THEY (the asker) have\n"
        "QUERY_MATH — asking for a math calculation/expression to be solved\n"
        "FETCH_MEDIA — asking to be sent a picture/image/video/gif\n"
        "FETCH_MUSIC — asking to be sent/played a song/track/music\n"
        "GENERAL_CHAT — anything else (casual conversation, questions, etc.)\n\n"
        "If the message is about staff/mod roles specifically, use "
        "QUERY_STAFF, not QUERY_ROLE.\n\n"
        "Output exactly one of: QUERY_STAFF, QUERY_ROLE, QUERY_MATH, "
        "FETCH_MEDIA, FETCH_MUSIC, GENERAL_CHAT"
    )

    loop = asyncio.get_running_loop()

    def _call():
        # 🌸 This is the BLOCKING sync SDK call — must never run
        # directly on the event loop. See run_in_executor below.
        return service.client.chat.completions.create(
            model=CLASSIFIER_MODEL,  # 🌸 openai/gpt-oss-20b — cheap pool model
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            # 🌸 CLASSIFIER_MODEL is a REASONING model — reasoning_effort
            # "low" keeps it from burning the whole max_tokens budget on
            # hidden chain-of-thought before ever emitting the visible
            # label (same fix already applied to classify_server_query/
            # classify_search_intent above).
            reasoning_effort="low",
            max_tokens=150,
        )

    try:
        response = await loop.run_in_executor(None, _call)
        label = (response.choices[0].message.content or "").strip().upper()
        if not label:
            print("⚠️ Intent classifier returned EMPTY content — defaulting to GENERAL_CHAT")
            return "GENERAL_CHAT"

        # 🌸 Defensive parse: strip stray punctuation/quotes/whitespace
        # the model sometimes wraps a single-word answer in, then take
        # just the first token in case it still adds a trailing word.
        cleaned = re.sub(r"[^A-Z_]", " ", label).split()
        label = cleaned[0] if cleaned else ""

        if label not in labels:
            return "GENERAL_CHAT"
        return label
    except GroqRateLimitError as e:
        # 🌸 Fail-open on a genuine 429 — never let a rate-limited
        # classifier call block the user from getting ANY reply.
        print(f"⚠️ Intent classifier rate-limited (falling back to GENERAL_CHAT): {e}")
        return "GENERAL_CHAT"
    except Exception as e:
        # 🌸 Network blips, timeouts, malformed responses, anything
        # else — same fail-open contract as every other classifier
        # in this file.
        print(f"⚠️ Intent classifier error (falling back to GENERAL_CHAT): {e}")
        return "GENERAL_CHAT"


async def extract_member_name(service, dispatch_text: str, username: str) -> str:
    """
    🌸 AI-FIRST ENTITY EXTRACTION for "member_lookup"-labeled messages —
    used by groq_service.py's _try_ai AFTER classify_server_query has
    already picked "member_lookup" as the topic, to figure out WHO the
    message is actually asking about.

    This exists specifically because a plain regex on message.content
    (handle_member_lookup_query's own MEMBER_LOOKUP_PATTERN_A/B/C fallback
    in groq_instruct.py) can't resolve a dangling pronoun follow-up like
    "does he on this server" — "he" only means something once you know
    the PREVIOUS message named "moonaesthetic0435". dispatch_text already
    has that prior message folded in (see groq_service.py's
    _PERSON_REFERENTIAL_PATTERN fold-in), so an AI call that reads the
    WHOLE dispatch_text can resolve the pronoun the same way a person
    reading the thread would, instead of a regex ever having a shot at
    it. Same "AI gets first crack" philosophy as classify_server_query
    elsewhere in this file — just one level deeper (WHO, not just WHAT
    kind of question).

    Returns the extracted name/username as a bare string, or "" (empty)
    on ANY problem — no client, no dispatch_text, API error, timeout,
    empty reply, or the model saying it can't identify anyone — so the
    caller (handle_member_lookup_query, via its name_hint param) always
    fails open to its own regex-on-message.content path, same
    "never worse than falling through" contract as every other
    classifier in this file.
    """
    if not service.client or not dispatch_text:
        return ""

    system_prompt = (
        "A Discord bot needs to know WHO a message is asking about. "
        "Read the text below — it may include an earlier message for "
        "context, followed by the latest message — and output ONLY the "
        "username/name of the ONE specific person being asked about, "
        "resolving any pronoun (he/she/they/him/her/them) to whoever was "
        "actually named earlier in the text. Output nothing else: no "
        "punctuation, no quotes, no explanation. If no specific person "
        "can be identified, output exactly: NONE\n\n"
        "The earlier context line may be the BOT's own previous reply "
        "(e.g. it may say something like 'X is here on this server'). "
        "Do NOT extract the bot's name from its own past reply just "
        "because it appears in the context — only extract a name if the "
        "LATEST message is itself asking about a specific person. A "
        "latest message that is just a greeting, a bare mention, or has "
        "no identifiable person of its own is NONE, even if a name is "
        "sitting right there in the earlier context line."
    )

    async def _call():
        return await service.async_client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": dispatch_text},
            ],
            temperature=0,
            # 🌸 Same reasoning-model fix as every classifier above —
            # reasoning_effort="low" + enough max_tokens for a short
            # reasoning pass + the name.
            reasoning_effort="low",
            max_tokens=150,
        )

    try:
        response = await _call()
        name = (response.choices[0].message.content or "").strip()
        if not name or name.strip().upper() == "NONE":
            return ""
        # 🌸 Defensive strip — model sometimes wraps the name in quotes/
        # backticks/a trailing period despite the "nothing else" instruction.
        return name.strip("`\"'.\n\t ").lstrip("@")
    except Exception as e:
        print(f"⚠️ Member-name extraction error (falling back to regex) for {username}: {e}")
        return ""
