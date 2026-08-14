import os
import sys
import re
import json
import asyncio
import logging

import discord
from ytmusicapi import YTMusic

# 🌸 key_config.py lives at auth/key_config.py, gitignored — same
# convention groq_ai.py / groq_exa_search.py use.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

try:
    from groq import Groq
except Exception:
    Groq = None

# 🌸 Same rationale as CATEGORY_CLASSIFIER_MODEL in groq_exa_search.py —
# this is a structured summarize/label task, not a chat reply, so the
# smallest/fastest model in the pool is the right shape, not a big one.
MUSIC_SUMMARY_MODEL = "llama-3.1-8b-instant"

MUSIC_SUMMARY_SYSTEM_PROMPT = (
    "You are a music search assistant for a kawaii-themed Discord bot. "
    "You will be given a JSON list of YouTube Music search results "
    "(title, artist, album, duration, type). Write a short, friendly "
    "1-2 sentence summary of what was found, in the bot's cute voice "
    "(you can use a light emoji or two, not more than 2). Do NOT list "
    "every track — just describe the vibe/spread of results (e.g. genre, "
    "top artist, mix of songs vs albums). No markdown, no headers, no "
    "preamble like 'Here is a summary'.\n\n"
    "Respond with ONLY a single-line JSON object, nothing else, no "
    "markdown fences, in exactly this shape:\n"
    '{"summary": "<your 1-2 sentence summary>"}'
)

# 🌸 Zero-token LOCAL keyword gate, same rationale as server_hint /
# _looks_server_related in groq_instruct.py — checked BEFORE spending a
# Groq call on classify_music_intent, so ordinary chat with no music
# keywords at all ("lol how are u") never costs an API call. This only
# gates the CLASSIFIER call, same as server_hint — it doesn't decide
# the final answer by itself.
MUSIC_INTENT_PATTERN = re.compile(
    r"\b(song|songs|music|track|tracks|album|albums|playlist|playlists|"
    r"lagu|musik|lagunya)\b",
    re.IGNORECASE,
)

# 🌸 Same "AI-first, regex-fallback" shape as classify_search_intent /
# _wants_web_search in groq_ai.py. Kept deliberately narrow — this only
# needs to answer "is this person asking me to FIND/SUGGEST music"
# (true/false), not extract the query itself; the caller still passes
# the raw message text to ytmusicapi's own search.
MUSIC_INTENT_CLASSIFIER_MODEL = "llama-3.1-8b-instant"

# 🌸 Combined ytm.search + Groq fallback: the raw message is tried as
# a literal search FIRST (free, no API call, handles the common case
# where a real title/artist is named). Groq is only invoked when that
# literal search comes back empty — at that point it's a *targeted*
# rewrite ("this exact search failed, suggest a better one"), not a
# blind guess, so it stays grounded in what ytmusicapi actually has
# rather than inventing something that may not exist.
QUERY_REWRITE_MODEL = "llama-3.1-8b-instant"

QUERY_REWRITE_SYSTEM_PROMPT = (
    "A Discord user asked for music, but searching YouTube Music for "
    "their message as-is returned NO results (their message likely "
    "has no clean literal title in it, e.g. 'give me a cool song' or "
    "'give me the link of the song').\n"
    "Rewrite this into a short, concrete, literally searchable YouTube "
    "Music query — a real, specific, popular song or artist name that "
    "matches any mood/genre/artist hinted at in the message, or just a "
    "well-known popular song if nothing is hinted at. Do NOT return "
    "genre words alone or a copy of the user's sentence — return an "
    "actual song or artist name likely to exist on YouTube Music.\n"
    "Respond with ONLY the search query text, nothing else — no "
    "quotes, no labels, no explanation."
)

MUSIC_INTENT_CLASSIFIER_POLICY = (
    "Decide if the USER MESSAGE is asking to find, search for, "
    "recommend, or suggest a song/track/album/playlist on YouTube "
    "Music — as opposed to ordinary chat that merely mentions music in "
    "passing (e.g. 'I love music' is NOT a request; 'find me some "
    "lofi beats' IS a request; 'give me a cool song' IS a request even "
    "with no specific title named — treat any vague ask for a music "
    "recommendation the same as a specific one).\n"
    "Respond with ONLY one word, lowercase, nothing else: "
    '"yes" or "no".'
)

# 🌸 Dedicated file logger, same pattern as exa_logger in
# groq_exa_search.py — one line per search so usage is traceable in
# logs/bot.log without cluttering stdout. The `if not logger.handlers`
# guard keeps this safe against duplicate handlers on reimport.
os.makedirs("logs", exist_ok=True)
music_logger = logging.getLogger("music_suggestion")
music_logger.setLevel(logging.INFO)
if not music_logger.handlers:
    _music_log_handler = logging.FileHandler("logs/bot.log", encoding="utf-8")
    _music_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    music_logger.addHandler(_music_log_handler)
    music_logger.propagate = False


class MusicSuggestionService:
    """
    🌸 Searches YouTube Music via ytmusicapi (no auth needed — same
    unauthenticated YTMusic() pattern music_downloader.py already uses
    for its autocomplete) and hands the results to a cheap Groq call
    that writes a short, cute, human-readable summary of what was found.

    Returns everything as a single JSON-shaped dict so callers (a cog,
    a slash command, a groq_instruct.py interceptor, whatever) can just
    dump the results straight into an embed or json.dumps() it as-is.

    Both YTMusic() init and the .search() call degrade quietly on
    failure, same "best-effort fallback" convention as
    groq_exa_search.py — a hiccup here should never take down a
    message handler.
    """

    def __init__(self):
        try:
            self.ytm = YTMusic()
            print("🌸 YTMusic search online (no auth needed)!")
        except Exception as e:
            self.ytm = None
            print(f"⚠️ YTMusic init failed, music suggestions disabled: {e}")

        # 🌸 Own lightweight Groq client, same self-contained convention
        # as ExaSearchService.groq_client in groq_exa_search.py — this
        # module doesn't depend on groq_ai.py's rotating-key GroqService
        # loading first.
        groq_key = getattr(key_config, "GROQ_API_KEYS", None)
        if isinstance(groq_key, (list, tuple)):
            groq_key = groq_key[0] if groq_key else None
        self.groq_client = Groq(api_key=groq_key) if (Groq and groq_key) else None

    def _search_sync(self, query: str, filter_type: str, limit: int) -> list:
        """🌸 Blocking ytmusicapi call — always run through
        asyncio.to_thread from the async methods below, never called
        directly, matching this codebase's asyncio.to_thread convention
        for blocking SDK calls."""
        return self.ytm.search(query, filter=filter_type, limit=limit)

    @staticmethod
    def _format_result(res: dict) -> dict:
        """🌸 Flattens one raw ytmusicapi result dict into a compact,
        stable shape — raw results vary a lot by result type (song vs
        album vs artist vs video), so this normalizes the fields callers
        actually care about and drops the rest."""
        artists = ", ".join(a.get("name", "") for a in res.get("artists", []) or []) or None

        album = res.get("album")
        if isinstance(album, dict):
            album = album.get("name")

        duration = res.get("duration")  # e.g. "3:45", None for albums/artists

        thumbnails = res.get("thumbnails") or []
        thumbnail_url = thumbnails[-1]["url"] if thumbnails else None

        return {
            "title": res.get("title") or res.get("artist") or "Unknown",
            "artist": artists,
            "album": album,
            "duration": duration,
            "type": res.get("resultType"),
            "video_id": res.get("videoId"),
            "browse_id": res.get("browseId"),  # albums/artists use this instead of videoId
            "url": f"https://www.youtube.com/watch?v={res['videoId']}" if res.get("videoId") else None,
            "thumbnail": thumbnail_url,
        }

    async def _summarize_with_groq(self, query: str, results: list) -> str:
        """🌸 One cheap Groq call → short cute summary string. Falls
        back to a plain templated sentence (no AI) on ANY problem — no
        client, API error, timeout, malformed JSON — same
        degrades-quietly convention as classify_categories() in
        groq_exa_search.py, so a Groq hiccup never breaks the search
        results themselves."""
        fallback = (
            f"Found {len(results)} result(s) for \"{query}\"! 🎶"
            if results else f"Couldn't find anything for \"{query}\". 🥺"
        )
        if not self.groq_client or not results:
            return fallback

        # 🌸 Trim to just the fields the model needs — keeps the prompt
        # small since this is a summarize-only call, not a chat reply.
        compact = [
            {k: r[k] for k in ("title", "artist", "album", "duration", "type")}
            for r in results
        ]

        def _call():
            return self.groq_client.chat.completions.create(
                model=MUSIC_SUMMARY_MODEL,
                messages=[
                    {"role": "system", "content": MUSIC_SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"query": query, "results": compact})},
                ],
                temperature=0.4,
                max_tokens=120,
            )

        try:
            response = await asyncio.to_thread(_call)
            raw = (response.choices[0].message.content or "").strip()
            # 🌸 Defensive strip: some models wrap JSON in ```json fences
            # even when told not to — same belt-and-suspenders approach
            # as classify_categories() in groq_exa_search.py.
            raw = raw.strip("`").removeprefix("json").strip()
            parsed = json.loads(raw)
            summary = str(parsed.get("summary", "")).strip()
            return summary or fallback
        except Exception as e:
            print(f"⚠️ Music summary Groq call failed (using fallback): {e}")
            return fallback

    async def classify_music_intent(self, prompt: str) -> bool:
        """🌸 One cheap Groq call → True/False, same shape as
        classify_search_intent in groq_ai.py. Fails CLOSED (returns
        False) on any problem — no client, API error, timeout, junk
        reply — so a classifier hiccup just means the message falls
        through to the normal AI reply chain instead of accidentally
        hijacking every mention as a music search."""
        if not self.groq_client or not prompt:
            return False

        def _call():
            return self.groq_client.chat.completions.create(
                model=MUSIC_INTENT_CLASSIFIER_MODEL,
                messages=[
                    {"role": "system", "content": MUSIC_INTENT_CLASSIFIER_POLICY},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=16,
            )

        try:
            response = await asyncio.to_thread(_call)
            raw = (response.choices[0].message.content or "").strip().lower()
            return raw.startswith("yes")
        except Exception as e:
            print(f"⚠️ Music intent classifier error (defaulting to no): {e}")
            return False

    async def rewrite_search_query(self, prompt: str) -> str | None:
        """🌸 Called ONLY after a literal ytm.search(prompt) already
        came back empty — asks Groq for a concrete, real song/artist
        name to retry with instead. Returns None (not the raw prompt)
        on any problem, so the caller knows to give up honestly rather
        than retrying with something no better than what already
        failed."""
        if not self.groq_client or not prompt:
            return None

        def _call():
            return self.groq_client.chat.completions.create(
                model=QUERY_REWRITE_MODEL,
                messages=[
                    {"role": "system", "content": QUERY_REWRITE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                max_tokens=32,
            )

        try:
            response = await asyncio.to_thread(_call)
            raw = (response.choices[0].message.content or "").strip().strip('"')
            return raw or None
        except Exception as e:
            print(f"⚠️ Query rewrite error (giving up on search): {e}")
            return None

    async def suggest(self, query: str, filter_type: str = "songs", limit: int = 8) -> dict:
        """🌸 Main entry point. Searches YouTube Music for `query` and
        returns a single JSON-serializable dict:

            {
                "query": "...",
                "filter": "songs",
                "count": 5,
                "results": [ {title, artist, album, duration, type,
                               video_id, browse_id, url, thumbnail}, ... ],
                "summary": "<cute AI summary>",
            }

        `filter_type` matches ytmusicapi's own `filter` param — one of
        "songs", "videos", "albums", "artists", "playlists",
        "community_playlists", "featured_playlists", "uploads". Defaults
        to "songs" since that's what the existing autocomplete in
        music_downloader.py already uses.

        On any failure (no ytm client, empty query, API error) returns
        the same shape with an empty results list and an apologetic
        summary, so callers never need a special-case branch — they can
        always just read result["results"] and result["summary"].
        """
        query = (query or "").strip()
        if not query or not self.ytm:
            return {
                "query": query,
                "filter": filter_type,
                "count": 0,
                "results": [],
                "summary": "I couldn't search for music right now, sorry! 🥺",
            }

        try:
            raw_results = await asyncio.to_thread(self._search_sync, query, filter_type, limit)
        except Exception as e:
            print(f"⚠️ YTMusic search failed for {query!r}: {e}")
            music_logger.info(f"[suggest] query={query!r} filter={filter_type} FAILED: {e}")
            raw_results = []

        results = [self._format_result(r) for r in raw_results if r]
        summary = await self._summarize_with_groq(query, results)

        music_logger.info(
            f"[suggest] query={query!r} filter={filter_type} -> {len(results)} result(s)"
        )

        return {
            "query": query,
            "filter": filter_type,
            "count": len(results),
            "results": results,
            "summary": summary,
        }


# 🌸 Module-level singleton, same convention as ExaSearchService's
# `exa = ExaSearchService()` instantiation pattern — one YTMusic/Groq
# client shared across every call site instead of re-init'ing per
# message. Import failures inside __init__ already degrade quietly
# (self.ytm / self.groq_client become None), so this line itself never
# raises.
music_service = MusicSuggestionService()

# 🌸 Same "🎧 kawaii player" thumbnail slot as SUCCESS_THUMBNAIL_URL in
# groq_ai.py — swap for your own asset if you've got one, this is just
# a safe default so the embed doesn't look bare.
MUSIC_THUMBNAIL_URL = "https://c.tenor.com/TcMXxO_U0dgAAAAC/tenor.gif"

# 🌸 Discord embed color — soft pink, matches the bot's kawaii branding
# used elsewhere (pink/purple gradient).
MUSIC_EMBED_COLOR = 0xFFB6D9


def _build_music_embed(result: dict) -> discord.Embed:
    """🌸 Turns a suggest() result dict into a discord.Embed — same
    shape as the embeds handle_media_request/Pexels/Pixabay build:
    set_author for the top-line summary, one field per top track,
    thumbnail for a bit of visual flair. Kept as its own function (not
    inlined into handle_music_request) so a cog could call it directly
    too, e.g. for a /music-search slash command reusing the same
    result dict."""
    embed = discord.Embed(
        title="🎶 Music Search",
        description=result["summary"],
        color=MUSIC_EMBED_COLOR,
    )
    embed.set_thumbnail(url=MUSIC_THUMBNAIL_URL)

    # 🌸 Cap at 5 fields so the embed never gets too tall for chat —
    # the AI summary above already covers the general vibe/spread of
    # ALL results, these are just the top few as quick clickable links.
    for track in result["results"][:5]:
        title = track["title"]
        artist = track.get("artist")
        name = f"{title} — {artist}" if artist else title
        value = track["url"] or track.get("album") or "—"
        embed.add_field(name=name[:256], value=value[:1024], inline=False)

    embed.set_footer(text=f"YouTube Music • \"{result['query']}\"")
    return embed


async def handle_music_request(message: "discord.Message", guild_id: int, shared) -> "discord.Embed | None":
    """🌸 Interceptor entry point, same call contract as
    handle_media_request(message, guild_id, shared) in groq_pexels.py —
    slot this into the SAME spot in groq_service.py's interceptor chain
    (handle_mention_reaction), right alongside handle_media_request.

    `guild_id` and `shared` aren't used yet (music search has no
    per-guild state to read), but are accepted so the call signature
    matches every other interceptor in the chain and it can be added
    as a drop-in without special-casing the call site.

    Returns a discord.Embed on a hit, or None if this message doesn't
    look like a music request (or the classifier says no) — matching
    every other interceptor's "None = try the next thing" contract.
    """
    text = message.clean_content.strip()
    if not text:
        return None

    # 🌸 Same zero-token-gate-then-classify shape as the server_hint /
    # classify_server_query pairing in groq_service.py — MUSIC_INTENT_PATTERN
    # is the free local pre-filter, classify_music_intent is the one
    # paid Groq call, only spent when the pre-filter already thinks
    # this MIGHT be music-related.
    if not MUSIC_INTENT_PATTERN.search(text):
        return None

    wants_music = await music_service.classify_music_intent(text)
    if not wants_music:
        return None

    # 🌸 Try the raw message as a literal ytmusicapi search first — the
    # common case (a real title/artist named) is handled here with no
    # extra cost. Only reach for Groq below if that comes up genuinely
    # empty, e.g. vague asks like "give me a cool song" that have
    # nothing searchable in them — and even then, Groq's rewrite gets
    # verified against ytm.search() again before it's ever shown to
    # the user, so a bad guess just means "couldn't find it" rather
    # than a hallucinated result.
    result = await music_service.suggest(text)
    if not result["results"]:
        rewritten_query = await music_service.rewrite_search_query(text)
        if rewritten_query:
            result = await music_service.suggest(rewritten_query)

    if not result["results"]:
        # 🌸 Once the classifier commits to "this IS a music request",
        # we must not let it silently fall through to the generic chat
        # model — that's how you get hallucinated song titles/links.
        # An honest "couldn't find it" beats a made-up track every time.
        return discord.Embed(
            title="🎶 Music Search",
            description=f"Couldn't find anything for \"{text}\" 🥺 try naming the song/artist directly?",
            color=MUSIC_EMBED_COLOR,
        )

    return _build_music_embed(result)


# 🌸 Quick manual smoke test — run directly (`python groq_music_suggestion.py
# "some query"`) to sanity-check search + summary output without needing
# the bot running. Not imported/used by anything else.
if __name__ == "__main__":
    async def _main():
        service = MusicSuggestionService()
        q = " ".join(sys.argv[1:]) or "lofi hip hop"
        result = await service.suggest(q)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    asyncio.run(_main())
