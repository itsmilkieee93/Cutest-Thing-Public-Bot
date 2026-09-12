import re

# 🌸 Chat-model pool for the priority reply — one is picked at random
# (random.choice) on every turn that doesn't pass an explicit model_id, so
# the bot's "voice" varies a little between replies. Real Groq model IDs
# as of July 2026 (verified against console.groq.com/docs/models):
#   • openai/gpt-oss-120b — OpenAI's 120B open-weight MoE, Groq's current
#     flagship, fastest+highest quality of the three.
#   • qwen/qwen3-32b      — dense 32B reasoning/chat model. NOTE: Groq
#     announced this deprecated on 2026-06-17, shutting down ~August 2026 —
#     keep an eye on console.groq.com/docs/deprecations and swap it out
#     for openai/gpt-oss-120b when it goes.
#   • qwen/qwen3.6-27b    — newer multimodal Qwen release (note the dot,
#     not a dash — "qwen3.6", not "qwen3-3.6"), currently a Groq preview
#     model (fine for a personal bot, just not "production-grade" per Groq).
#   • groq/compound       — agentic SYSTEM (not a plain model) built on
#     GPT-OSS 120B + Llama 4 Scout, with built-in web search (Tavily) that
#     fires automatically whenever the model decides a query needs current
#     info. This is the ONLY way to get real-time web search on Groq — it's
#     baked into these two system IDs, not a flag you can bolt onto
#     gpt-oss/qwen/llama above. See console.groq.com/docs/compound.
#   • groq/compound-mini  — same idea, smaller/faster/cheaper, single search
#     tool call per turn (vs. compound's ability to chain multiple).
# NOTE: compound systems reject reasoning_format/reasoning_effort (400
# error) — _call() in groq_ai.py branches on "compound" in model_lower to
# skip those params, same as the existing llama branch.
MODEL_POOL = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "groq/compound",
    "groq/compound-mini",
]

# 🌸 SEARCH-INTENT GATE — cheap regex pre-filter so "search for X" /
# "look up X" / "what's the latest on X" etc. reliably route to
# groq/compound-mini instead of leaving it to random.choice(MODEL_POOL)
# luck (~29% with 2 compound entries in a 7-model pool). Mirrors the same
# gate+regex-fallback pattern used for server-query routing elsewhere —
# cheap, no LLM call needed, just a keyword/phrase net. compound-MINI
# (not compound) is the forced pick here: single search tool call is
# plenty for "user asked me to look something up" and it's the
# faster/cheaper of the two systems. Bare compound stays in MODEL_POOL
# for the random rotation to occasionally surface on its own.
#
# Deliberately broad rather than narrow — false positives just mean an
# ordinary chat reply comes from compound-mini instead of another pool
# model (harmless, compound-mini still chats fine when it decides no
# search is needed), whereas false negatives mean a real search request
# silently falls through to a model with zero web access.
SEARCH_INTENT_PATTERN = re.compile(
    r"\b("
    r"search(?:\s+(?:for|up|the\s+web|online))?"
    r"|(?:look|check)\s+(?:up|online)"
    r"|google\s+(?:it|that|this|for)"
    r"|what'?s\s+(?:the\s+)?latest"
    r"|(?:current|latest|recent|today'?s|this\s+week'?s)\s+(?:news|price|score|update|weather|version)"
    r"|(?:is|are)\s+.+\s+still\s+(?:alive|around|a\s+thing|available|working)"
    r"|who\s+(?:is|are)\s+the\s+current"
    r"|right\s+now\b.*\?"
    r")\b",
    re.IGNORECASE,
)


def _wants_web_search(prompt: str) -> bool:
    """🌸 REGEX FALLBACK — True if `prompt` looks like the user wants
    current/real-world info Groq's non-compound models can't know
    (post-training-cutoff facts, prices, scores, news, "is X still a
    thing" type questions). Used in groq_ai.py's get_ai_response as the
    2nd-choice check, ONLY when classify_search_intent (the AI-first
    router below) errors, times out, or comes back "no" — same
    AI-first/regex-fallback shape as classify_server_query does for
    server-info questions."""
    if not prompt:
        return False
    return bool(SEARCH_INTENT_PATTERN.search(prompt))


# 🌸 SEARCH-INTENT CLASSIFIER POLICY — AI-FIRST router, same pattern as
# SERVER_QUERY_CLASSIFIER_POLICY above: one cheap Groq call (smallest
# model in MODEL_POOL, max_tokens=4, temperature=0) decides whether THIS
# message needs a live web search, so paraphrases the regex net would
# miss ("did they release the sequel yet", "how's Bitcoin doing rn",
# "wonder if that place is still open") still route to Exa correctly.
# SEARCH_INTENT_PATTERN (regex) is kept as the FALLBACK — tried only
# when this classifier errors/times out/returns junk — never removed,
# only skipped when the AI call already succeeded. See
# classify_search_intent in groq_ai.py for the call site, and
# get_ai_response's model-selection block for how the two combine.
SEARCH_INTENT_CLASSIFIER_POLICY = (
    "Classify whether the USER MESSAGE needs a LIVE web search to answer "
    "correctly (current events, prices, scores, news, release dates, "
    "\"is X still around/available\", weather, or anything that could've "
    "changed after an AI's training cutoff).\n"
    "Reply YES if it needs current/real-world info to answer well. "
    "Reply NO for normal chat, opinions, jokes, general knowledge, math, "
    "code, or anything about the bot/server itself.\n"
    "Respond with EXACTLY one word, nothing else: YES or NO."
)


# 🌸 SEARCH DOMAIN ALLOWLISTS — restricting compound's search_settings.
# include_domains to a small trusted set per topic does TWO things at
# once: (1) Tavily returns fewer/smaller/more relevant snippets, which
# directly cuts the token cost that was tipping compound-mini into 413
# ("Request Entity Too Large") on a thin TPM budget, and (2) answer
# quality goes up since the model isn't sifting low-quality SEO spam
# for things like sports scores or weather. Order matters — first
# matching topic in SEARCH_TOPIC_DOMAINS wins, so put narrower/more
# specific topics before broader catch-alls.
#
# Deliberately small lists (2-4 domains each) — Tavily's include_domains
# supports wildcards ("*.gov") but we're not using them here since the
# goal is fewer/smaller results, not broader coverage. If a topic's
# real answer isn't on any of these, compound will just come back
# empty/thin rather than making something up from a low-quality site,
# which is the safer failure mode for a Discord bot.
SEARCH_TOPIC_DOMAINS = [
    # (label, keyword pattern, include_domains list)
    (
        "sports",
        re.compile(
            r"\b(world\s*cup|fifa|olympic|nba|nfl|premier\s*league|"
            r"champions\s*league|match|tournament|score|final\s*score|"
            r"who\s+won|who\s+winning|standings)\b",
            re.IGNORECASE,
        ),
        ["fifa.com", "espn.com", "wikipedia.org", "bbc.com"],
    ),
    (
        "weather",
        re.compile(r"\bweather|forecast|temperature|humidity|rain(?:y|fall)?\b", re.IGNORECASE),
        ["weather.com", "accuweather.com", "bmkg.go.id"],
    ),
    (
        "news",
        re.compile(r"\bnews|headline|breaking|happened\s+(?:today|this\s+week)\b", re.IGNORECASE),
        ["reuters.com", "apnews.com", "bbc.com"],
    ),
    (
        "reference",
        re.compile(
            r"\bwho\s+is\s+the\s+current|what\s+year|how\s+old\s+is|"
            r"population\s+of|capital\s+of\b",
            re.IGNORECASE,
        ),
        ["wikipedia.org"],
    ),
]


def _search_domains_for_prompt(prompt: str) -> list[str] | None:
    """🌸 Picks a small include_domains allowlist for compound's
    search_settings based on keywords in `prompt` — see
    SEARCH_TOPIC_DOMAINS above. Returns None (no restriction) if the
    prompt doesn't match any known topic, so genuinely open-ended
    search questions still get compound's normal unrestricted search
    rather than an empty/wrong-topic allowlist forcing a bad answer.
    First matching topic wins; used in groq_ai.py's _call() to build
    kwargs["search_settings"] only for compound/compound-mini models."""
    if not prompt:
        return None
    for _label, pattern, domains in SEARCH_TOPIC_DOMAINS:
        if pattern.search(prompt):
            return domains
    return None


# 🌸 SERVER-QUERY CLASSIFIER — AI-FIRST router for the server-info regex
# interceptors above. Runs BEFORE the regex chain (not instead of it):
# one cheap Groq call decides which server-info handler (if any) the
# message is asking for, so a paraphrase like "who owns this place" or
# "how old is this discord" routes straight to the right handler without
# needing its own hand-written pattern. The regex chain in
# handle_mention_reaction is still tried as a FALLBACK whenever this
# classifier errors, times out, or returns a label it doesn't recognize —
# it is never removed, only skipped when the AI call already succeeded.
#
# Deliberately the smallest/fastest model in MODEL_POOL (not a bigger
# model, and not a second SAFEGUARD-style reasoning model) — this is a
# single-word classification, so it's tuned for minimum input+output
# tokens rather than quality: short system prompt, no few-shot examples,
# max_tokens=6, temperature=0.
CLASSIFIER_MODEL = "openai/gpt-oss-20b"
