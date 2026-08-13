import os
import sys
import json
import logging
import asyncio
import urllib.parse

import aiohttp

from exa_py import Exa

# 🌸 key_config.py lives at auth/key_config.py, gitignored — same
# convention groq_ai.py uses. Path is relative to CWD (bot root), matching
# this codebase's existing convention of relative paths like "auth/..."
# instead of __file__-based ones.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

# 🌸 Groq client for the Currents+Jina fallback path's own summarize
# step (see _summarize_with_groq below) — a SEPARATE lightweight client
# from groq_ai.py's rotating-key GroqService, same pattern
# groq_exa_search.py already uses for Exa itself: this module is fully
# self-contained and doesn't depend on groq_ai.py loading first.
try:
    from groq import Groq
except Exception:
    Groq = None

# 🌸 r.jina.ai READER — free, no key required for basic use. Prepend
# "https://r.jina.ai/" to any URL and it fetches + converts that page
# into clean, LLM-friendly plain text server-side (handles JS rendering,
# strips nav/ads/boilerplate). Used here as a THIRD-tier fallback (after
# Exa's answer()/search_summary() both come back empty) — pulls the
# full text of a Currents news article through the reader, then
# Groq-summarizes it down to something Exa-answer-sized before it's
# handed back to the caller, so it slots into the exact same
# short-string contract get_answer()/search_summary() already have.
JINA_READER_BASE = "https://r.jina.ai/"

# 🌸 Same rationale as CLASSIFIER_MODEL in groq_instruct.py — this is a
# single summarization call, not a chat reply, so pick the
# smallest/fastest model rather than a bigger one.
JINA_SUMMARY_MODEL = "llama-3.1-8b-instant"

JINA_SUMMARY_SYSTEM_PROMPT = (
    "Summarize the following news article text into a short, factual "
    "answer for a Discord AI bot's reply. 3-4 sentences max. No "
    "headers, no preamble like 'Here is a summary' — just the answer."
)

# 🌸 CATEGORY CLASSIFIER — one cheap Groq call picks the best-fit
# category for BOTH Exa's search() and Currents' /v1/search at once,
# returned as a single JSON object so it's one round-trip instead of
# two. Same rationale as CLASSIFIER_MODEL in groq_instruct.py: this is
# a structured-label task, not a chat reply, so the smallest/fastest
# model + temperature=0 + a tight max_tokens is the right shape.
#
# Both taxonomies are each source's OWN canonical list (looked up
# directly from their docs, not guessed):
#   • EXA_CATEGORIES    — from Exa's /search `category` param docs
#     (docs.exa.ai/reference/search). "company"/"people" disable several
#     other filters on Exa's side, but that's Exa's own tradeoff to
#     make, not something this classifier needs to special-case.
#   • CURRENTS_CATEGORIES — Currents' V2 canonical taxonomy, the
#     authoritative list per /v2/available/categories
#     (currentsapi.services/en/docs). V1 category values differ
#     slightly (e.g. "technology" instead of "science_technology") but
#     Currents' backend normalizes legacy labels, so V2 names are safe
#     to send even against the /v1/search endpoint _fetch_currents_url
#     already calls.
EXA_CATEGORIES = [
    "company", "research paper", "news", "pdf", "github",
    "personal site", "linkedin profile", "financial report",
    "people", "tweet",
]

CURRENTS_CATEGORIES = [
    "general", "society", "science_technology", "politics_government",
    "economy_business_finance", "arts_culture_entertainment",
    "lifestyle_leisure", "human_interest", "sport", "crime_law_justice",
    "education", "environment", "labour", "health", "automotive",
    "real_estate",
]

# 🌸 Same smallest/fastest model as JINA_SUMMARY_MODEL — this call
# returns a tiny JSON object, no reasoning needed.
CATEGORY_CLASSIFIER_MODEL = "llama-3.1-8b-instant"

CATEGORY_CLASSIFIER_POLICY = (
    "Classify the USER QUERY into the single best-fit category from "
    "EACH of the two lists below, for two different search APIs.\n\n"
    f"EXA_CATEGORIES (pick exactly one): {', '.join(EXA_CATEGORIES)}\n"
    f"CURRENTS_CATEGORIES (pick exactly one): {', '.join(CURRENTS_CATEGORIES)}\n\n"
    "If the query is ordinary chat/current-events/general knowledge "
    "with no clear specialized fit, use \"news\" for exa_category and "
    "\"general\" for currents_category.\n"
    "Respond with ONLY a single-line JSON object, nothing else, no "
    "markdown fences, in exactly this shape:\n"
    '{"exa_category": "<one of EXA_CATEGORIES>", '
    '"currents_category": "<one of CURRENTS_CATEGORIES>"}'
)

# 🌸 Dedicated file logger, same pattern as groq_ai.py's groq_logger — one
# line per Exa call (query + which method answered it + char count), so you
# can track Exa usage over time in logs/bot.log without cluttering stdout.
# The `if not exa_logger.handlers` guard keeps this safe to import more
# than once (e.g. via importlib.reload) without stacking duplicate
# handlers and writing every line twice.
os.makedirs("logs", exist_ok=True)
exa_logger = logging.getLogger("exa_search")
exa_logger.setLevel(logging.INFO)
if not exa_logger.handlers:
    _exa_log_handler = logging.FileHandler("logs/bot.log", encoding="utf-8")
    _exa_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    exa_logger.addHandler(_exa_log_handler)
    exa_logger.propagate = False


class ExaSearchService:
    """
    🌸 Live web search via Exa (https://exa.ai) — used in groq_ai.py as the
    PRIMARY search path when a prompt looks like it wants current/
    real-world info (see _wants_web_search in groq_instruct.py), with
    Groq's own groq/compound-mini + browser_search chain kept as a
    fallback for whenever Exa itself is unavailable (no key, network
    blip, etc.).

    WHY EXA INSTEAD OF (OR ALONGSIDE) COMPOUND: compound/compound-mini's
    413 ("Request Entity Too Large") issue came from Tavily's raw search
    snippets getting fed back into the SAME Groq call that also has to
    generate the reply — the search round-trip and the chat completion
    share one thin TPM budget. Exa's answer() endpoint does the
    search-AND-summarize step OUTSIDE of Groq entirely and hands back a
    short, already-synthesized string. By the time that reaches Groq it's
    just an ordinary short piece of context, not a raw dump of page text —
    so the token-budget problem that caused this whole chain of fixes
    doesn't come up in the first place. get_answer()/search_summary() also
    hard-cap what they return (see max_chars params) so nothing unbounded
    ever gets handed to Groq, on top of that.
    """

    def __init__(self):
        # 🌸 EXA_API_KEY follows the same key_config.py convention as
        # key_config.GROQ_API_KEYS — expected as a single string. Also
        # accepts a list/tuple defensively (takes the first entry) in case
        # it's ever set up the same way as the rotating Groq key list.
        raw_key = getattr(key_config, "EXA_API_KEY", None)
        if isinstance(raw_key, (list, tuple)):
            raw_key = raw_key[0] if raw_key else None
        self.api_key = raw_key

        if self.api_key:
            self.client = Exa(api_key=self.api_key)
            print("🌸 Exa search online with a heart (API key found)!")
        else:
            self.client = None
            print("❌ ERROR: No EXA_API_KEY found in key_config — Exa search disabled, falling back to compound only.")

        # 🌸 r.jina.ai + Currents fallback's own Groq client — see
        # JINA_SUMMARY_MODEL up top. Optional: if GROQ_API_KEYS isn't set
        # or the groq package isn't installed, _summarize_with_groq just
        # falls back to trimmed raw text instead of an AI summary — this
        # fallback path is best-effort on top of an already-best-effort
        # fallback, so it degrades quietly rather than raising.
        groq_key = getattr(key_config, "GROQ_API_KEYS", None)
        if isinstance(groq_key, (list, tuple)):
            groq_key = groq_key[0] if groq_key else None
        self.groq_client = Groq(api_key=groq_key) if (Groq and groq_key) else None

        # 🌸 JINA_API_KEY (key_config) is optional — reader works keyless
        # at 20rpm, a key raises that to 500rpm. Same defensive
        # list/tuple handling as EXA_API_KEY above.
        jina_key = getattr(key_config, "JINA_API_KEY", None)
        if isinstance(jina_key, (list, tuple)):
            jina_key = jina_key[0] if jina_key else None
        self.jina_api_key = jina_key

    async def classify_categories(self, query: str) -> dict:
        """🌸 One cheap Groq call, returns
        {"exa_category": ..., "currents_category": ...} for `query` —
        see CATEGORY_CLASSIFIER_POLICY up top for the two taxonomies.
        Used by search_summary() (Exa) and _fetch_currents_url()
        (Currents) to narrow their results to the right content type
        instead of searching unfiltered every time.

        Returns {"exa_category": "news", "currents_category": "general"}
        — safe, broad defaults — on ANY problem (no groq client, API
        error, timeout, malformed/non-JSON reply, or a value outside
        either taxonomy), so a classifier hiccup degrades to today's
        unfiltered-search behavior instead of breaking the search
        entirely."""
        defaults = {"exa_category": "news", "currents_category": "general"}
        if not self.groq_client or not query:
            return defaults

        def _call():
            return self.groq_client.chat.completions.create(
                model=CATEGORY_CLASSIFIER_MODEL,
                messages=[
                    {"role": "system", "content": CATEGORY_CLASSIFIER_POLICY},
                    {"role": "user", "content": query},
                ],
                temperature=0,
                max_tokens=40,
            )

        try:
            response = await asyncio.to_thread(_call)
            raw = (response.choices[0].message.content or "").strip()
            # 🌸 Defensive strip: some models wrap JSON in ```json fences
            # even when told not to — same belt-and-suspenders approach
            # as _strip_reasoning in groq_instruct.py.
            raw = raw.strip("`").removeprefix("json").strip()
            parsed = json.loads(raw)

            exa_cat = str(parsed.get("exa_category", "")).strip().lower()
            currents_cat = str(parsed.get("currents_category", "")).strip().lower()

            if exa_cat not in EXA_CATEGORIES:
                exa_cat = defaults["exa_category"]
            if currents_cat not in CURRENTS_CATEGORIES:
                currents_cat = defaults["currents_category"]

            result = {"exa_category": exa_cat, "currents_category": currents_cat}
            exa_logger.info(f"[classify_categories] query={query!r} -> {result}")
            return result
        except Exception as e:
            print(f"⚠️ Category classifier error (using defaults): {e}")
            exa_logger.info(f"[classify_categories] query={query!r} FAILED: {e}")
            return defaults

    async def get_answer(self, query: str, max_chars: int = 800) -> str | None:
        """🌸 PREFERRED path — Exa's answer() endpoint returns a short,
        already-synthesized answer with citations baked in (Exa does the
        search + reading + summarizing server-side), so this is the
        smallest/cleanest thing we can hand back to Groq. Returns None on
        any failure (no key, network error, empty answer, etc.) so the
        caller can fall through to search_summary() or the compound chain.
        max_chars is a hard backstop trim on top of Exa's own summarizing
        — belt and suspenders, since an oversized answer here is exactly
        the kind of thing that caused the original 413s.
        """
        if not self.client:
            return None
        try:
            response = await asyncio.to_thread(self.client.answer, query)
            answer = getattr(response, "answer", None)
            if not answer:
                return None
            answer = answer.strip()
            if max_chars and len(answer) > max_chars:
                answer = answer[:max_chars].rsplit(" ", 1)[0] + "…"
            exa_logger.info(f"[answer] query={query!r} chars={len(answer)}")
            return answer
        except Exception as e:
            print(f"⚠️ Exa answer() error: {e}")
            exa_logger.info(f"[answer] query={query!r} FAILED: {e}")
            return None

    async def search_summary(
        self,
        query: str,
        num_results: int = 5,
        max_chars_per_result: int = 500,
        category: str | None = None,
    ) -> str | None:
        """🌸 FALLBACK when get_answer() comes back empty/fails but Exa
        itself is reachable — a plain search with highlights, manually
        trimmed to max_chars_per_result PER RESULT before it's ever handed
        to Groq. This is the client-side cap that keeps the total payload
        bounded (num_results * max_chars_per_result, ~900 chars at
        defaults) regardless of how much text Exa itself found, same
        spirit as get_answer()'s max_chars.

        `category` (one of EXA_CATEGORIES, see classify_categories) narrows
        results to a specific content type when provided — e.g. "news" for
        current-events queries, "research paper" for academic ones. Passed
        straight through as Exa's own `category` kwarg; omitted entirely
        when None so unfiltered search still works exactly as before.
        """
        if not self.client:
            return None
        try:
            kwargs = dict(
                type="auto",
                num_results=num_results,
                highlights={"num_sentences": 2, "highlights_per_url": 1},
            )
            if category:
                kwargs["category"] = category
            response = await asyncio.to_thread(
                self.client.search_and_contents,
                query,
                **kwargs,
            )
            lines = []
            for result in getattr(response, "results", None) or []:
                title = getattr(result, "title", None) or getattr(result, "url", "source")
                highlights = getattr(result, "highlights", None) or []
                text = " ".join(highlights).strip()
                if not text:
                    continue
                if len(text) > max_chars_per_result:
                    text = text[:max_chars_per_result].rsplit(" ", 1)[0] + "…"
                lines.append(f"- {title}: {text}")

            if not lines:
                exa_logger.info(f"[search_summary] query={query!r} category={category!r} EMPTY")
                return None

            summary = "\n".join(lines)
            exa_logger.info(f"[search_summary] query={query!r} category={category!r} results={len(lines)} chars={len(summary)}")
            return summary
        except Exception as e:
            print(f"⚠️ Exa search_and_contents() error: {e}")
            exa_logger.info(f"[search_summary] query={query!r} category={category!r} FAILED: {e}")
            return None

    async def _fetch_currents_url(self, session: aiohttp.ClientSession, query: str, language: str = "en", category: str | None = None) -> str | None:
        """🌸 Grabs the single best-matching Currents news article URL
        for `query` via their /search endpoint (same endpoint news_api.py
        could use for its /news command). Returns just the article URL —
        the actual content extraction is r.jina.ai's job here, not ours.
        Returns None if NEWS_API_KEY isn't set, the call fails, or
        nothing matched.

        `category` (one of CURRENTS_CATEGORIES, see classify_categories)
        narrows the search to a specific news category when provided —
        passed straight through as Currents' own `category` param, using
        the V2 canonical values which Currents' backend also accepts
        (normalized) on this /v1/search endpoint."""
        news_api_key = (getattr(key_config, "NEWS_API_KEY", "") or "").strip()
        if not news_api_key:
            return None
        url = (
            "https://api.currentsapi.services/v1/search"
            f"?keywords={urllib.parse.quote(query)}&language={language}"
            f"&apiKey={news_api_key}"
        )
        if category:
            url += f"&category={urllib.parse.quote(category)}"
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                articles = data.get("news", [])
                if not articles:
                    return None
                return articles[0].get("url")
        except Exception as e:
            print(f"⚠️ Currents search failed for {query!r}: {e}")
            return None

    async def _fetch_via_jina(self, session: aiohttp.ClientSession, url: str, max_chars: int = 3000) -> str | None:
        """🌸 Fetches `url` through r.jina.ai's reader and returns clean
        plain text, trimmed to max_chars before it's ever handed to Groq
        — same client-side cap philosophy as get_answer()/search_summary()
        above. Returns None on any failure (timeout, non-200, empty
        body) so the caller can fail through cleanly."""
        headers = {"X-Return-Format": "text"}
        if self.jina_api_key:
            headers["Authorization"] = f"Bearer {self.jina_api_key}"
        try:
            async with session.get(f"{JINA_READER_BASE}{url}", headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return None
                text = (await resp.text()).strip()
                if not text:
                    return None
                if len(text) > max_chars:
                    text = text[:max_chars].rsplit(" ", 1)[0] + "…"
                return text
        except Exception as e:
            print(f"⚠️ r.jina.ai fetch failed for {url}: {e}")
            return None

    async def _summarize_with_groq(self, query: str, article_text: str, max_chars: int = 800) -> str:
        """🌸 Folds a raw news article dump down into a short,
        Exa-answer-sized string — keeps this fallback's output the same
        SHAPE as get_answer()'s so the caller (search() below, and
        groq_ai.py beyond it) doesn't need to know which path answered.
        Falls back to plain trimmed text if Groq itself isn't configured
        or the call fails, same best-effort spirit as the rest of this
        fallback chain."""
        if not self.groq_client:
            return article_text[:max_chars]

        def _call():
            return self.groq_client.chat.completions.create(
                model=JINA_SUMMARY_MODEL,
                messages=[
                    {"role": "system", "content": JINA_SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Topic: {query}\n\n{article_text}"},
                ],
                temperature=0.3,
                max_tokens=200,
            )

        try:
            response = await asyncio.to_thread(_call)
            summary = (response.choices[0].message.content or "").strip()
            if not summary:
                return article_text[:max_chars]
            if len(summary) > max_chars:
                summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
            return summary
        except Exception as e:
            print(f"⚠️ Jina-fallback Groq summarize error: {e}")
            return article_text[:max_chars]

    async def jina_news_fallback(self, query: str, language: str = "en", currents_category: str | None = None) -> str | None:
        """🌸 THIRD-tier fallback — only reached from search() below when
        BOTH Exa methods come back empty (no key, Exa down, or a query
        Exa just didn't have a good answer for). Finds `query`'s best
        Currents news match, pulls that article's full text through
        r.jina.ai's reader, and Groq-summarizes that down to a short
        answer string. Returns None if the Currents lookup, the Jina
        fetch, OR both fail — at which point search()'s caller
        (groq_ai.py) falls through to groq/compound-mini exactly as
        before, so this fallback is purely additive and never blocks the
        existing chain.

        `currents_category` (one of CURRENTS_CATEGORIES, see
        classify_categories) is passed straight through to
        _fetch_currents_url to narrow the match."""
        if not query:
            return None
        async with aiohttp.ClientSession() as session:
            article_url = await self._fetch_currents_url(session, query, language, category=currents_category)
            if not article_url:
                exa_logger.info(f"[jina_news_fallback] query={query!r} category={currents_category!r} NO_CURRENTS_MATCH")
                return None
            article_text = await self._fetch_via_jina(session, article_url)
            if not article_text:
                exa_logger.info(f"[jina_news_fallback] query={query!r} JINA_FETCH_EMPTY url={article_url}")
                return None

        summary = await self._summarize_with_groq(query, article_text)
        exa_logger.info(f"[jina_news_fallback] query={query!r} url={article_url} chars={len(summary)}")
        return summary

    async def search(self, query: str) -> str | None:
        """🌸 Main entry point for groq_ai.py — tries get_answer() first
        (cheapest, cleanest, already-summarized), falls back to
        search_summary() if that comes back empty, then falls back to
        jina_news_fallback() (Currents + r.jina.ai + Groq summary) if
        THAT'S still empty, and returns None only if ALL THREE fail (no
        key, Exa is down, no matching Currents article either) — at
        which point the caller falls through to the existing Groq
        compound/browser_search chain instead.

        🌸 CATEGORY-AWARE: classify_categories runs ONCE up front (one
        cheap Groq call, ~40 output tokens) and its result is threaded
        through both Exa's search_summary() and Currents' search — so a
        query like "latest AI research papers" narrows Exa to
        category="research paper" and Currents to
        category="science_technology" instead of both searching
        unfiltered. get_answer() is skipped for category filtering since
        Exa's answer() endpoint doesn't accept a category param at all —
        it stays exactly as before.
        """
        if not query:
            return None

        categories = await self.classify_categories(query)

        if self.client:
            answer = await self.get_answer(query)
            if answer:
                return answer
            summary = await self.search_summary(query, category=categories["exa_category"])
            if summary:
                return summary
        return await self.jina_news_fallback(query, currents_category=categories["currents_category"])
