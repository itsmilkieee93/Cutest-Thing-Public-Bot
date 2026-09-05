import os
import sys
import re
import json
import time
import asyncio
import logging

import discord
from discord.ext import commands
import aiosqlite
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
MUSIC_SUMMARY_MODEL = "openai/gpt-oss-20b"

MUSIC_SUMMARY_SYSTEM_PROMPT = (
    "You are a music search assistant for a kawaii-themed Discord bot. "
    "You will be given a JSON list of 2 or more YouTube Music search "
    "results (title, artist, album, duration, type). Write a short, "
    "friendly 1-2 sentence summary describing the overall vibe/spread "
    "of results (e.g. genre, top artist, mix of songs vs albums), in "
    "the bot's cute voice (you can use a light emoji or two, not more "
    "than 2). No markdown, no headers, no preamble like 'Here is a "
    "summary'.\n\n"
    "CRITICAL: only mention artists/titles that literally appear in "
    "the given list — never invent, assume, or add any artist that "
    "isn't there. Do NOT list every track individually — just a short "
    "natural-language description of what's actually present.\n\n"
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
# 🌸 openai/gpt-oss-20b — Groq's official replacement for the
# deprecated llama-3.1-8b-instant (shut down Aug 16, 2026). It's a
# REASONING model though (unlike llama-3.1-8b-instant), so it needs
# reasoning_effort="low" + enough max_tokens headroom below, or it can
# burn its whole token budget on invisible chain-of-thought and return
# an empty content string — that's exactly what happened during
# testing with max_tokens=16 and no reasoning_effort set: raw='' every
# time, which .startswith("yes") silently read as "no" forever.
MUSIC_INTENT_CLASSIFIER_MODEL = "openai/gpt-oss-20b"

# 🌸 Combined ytm.search + Groq fallback: the raw message is tried as
# a literal search FIRST (free, no API call, handles the common case
# where a real title/artist is named). Groq is only invoked when that
# literal search comes back empty — at that point it's a *targeted*
# rewrite ("this exact search failed, suggest a better one"), not a
# blind guess, so it stays grounded in what ytmusicapi actually has
# rather than inventing something that may not exist.
QUERY_REWRITE_MODEL = "openai/gpt-oss-20b"

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

# 🌸 Hard ceiling on how many tracks the embed/paginator will ever show,
# regardless of what the user asks for or what the AI decides — keeps a
# request like "give me 500 songs" from blowing up the ytmusicapi call,
# the Groq summary prompt size, or the paginator UI.
MAX_SUGGESTED_TRACKS = 20

# 🌸 Same tiny structured-decision shape as MUSIC_INTENT_CLASSIFIER_POLICY —
# one cheap Groq call, not a chat reply. Reads the user's message for an
# explicit number ("gimme 10 songs", "top 3 lofi tracks") and falls back
# to its own judgment (based on wording like "a song" vs "a playlist" vs
# "some songs") when no number is stated. Clamped again in code afterward
# so a malformed/out-of-range reply can never exceed MAX_SUGGESTED_TRACKS.
TRACK_COUNT_SYSTEM_PROMPT = (
    "A Discord user is asking for song/music suggestions. Decide how "
    "many tracks should be returned.\n"
    "- If the message states an explicit number (e.g. 'give me 10 "
    "songs', 'top 3 tracks'), use that number exactly.\n"
    "- If no number is stated, use your own judgment based on the "
    "wording: a single specific song ask ('play X', 'find me the song "
    "X') -> 1. A vague single-song ask ('give me a cool song') -> 1-3. "
    "A general request for 'some songs'/'a few tracks' -> 5. A request "
    "for 'a playlist', 'a bunch of songs', or 'lots of' -> 10-20.\n"
    f"Always respond with a whole number from 1 to {MAX_SUGGESTED_TRACKS} "
    "inclusive, never higher, never lower.\n"
    "Respond with ONLY a single-line JSON object, nothing else, no "
    "markdown fences, in exactly this shape:\n"
    '{"count": <integer>}'
)

# 🌸 Same "official replacement, needs reasoning_effort=low" rationale as
# MUSIC_INTENT_CLASSIFIER_MODEL above — this is a structured pick-a-number
# task, so the small/fast model is the right shape here too.
TRACK_COUNT_MODEL = "openai/gpt-oss-20b"

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
        # 🌸 Loud diagnostic — the old silent version of this line was
        # exactly why music requests were failing invisibly: no
        # GROQ_API_KEYS (or an empty list) in this bot's key_config.py
        # means self.groq_client is None, classify_music_intent()
        # returns False with NO exception/log line, and the message
        # just quietly falls through to the normal chat reply. Now
        # startup makes it obvious which case you're in.
        if self.groq_client:
            print("🌸 Music suggestion Groq client online!")
        elif not Groq:
            print("⚠️ Music suggestions disabled: groq package not installed")
        else:
            print("⚠️ Music suggestions disabled: no GROQ_API_KEYS found in key_config.py")

    def _search_sync(self, query: str, filter_type: str, limit: int) -> list:
        """🌸 Blocking ytmusicapi call — always run through
        asyncio.to_thread from the async methods below, never called
        directly, matching this codebase's asyncio.to_thread convention
        for blocking SDK calls."""
        return self.ytm.search(query, filter=filter_type, limit=limit)

    @staticmethod
    def _format_result(res: dict) -> dict:
        """🌸 Flattens one raw ytmusicapi result dict into a compact,
        stable shape, extracting maxresdefault thumbnail when possible."""
        artists = ", ".join(a.get("name", "") for a in res.get("artists", []) or []) or None

        album = res.get("album")
        if isinstance(album, dict):
            album = album.get("name")

        duration = res.get("duration")  # e.g. "3:45", None for albums/artists
        video_id = res.get("videoId")

        # 🌸 Fallback to the largest available native thumbnail
        thumbnails = res.get("thumbnails") or []
        thumbnail_url = thumbnails[-1]["url"] if thumbnails else None

        # 🌸 If it's a song/video with a valid videoId, build the maxresdefault CDN link
        if video_id:
            thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

        return {
            "title": res.get("title") or res.get("artist") or "Unknown",
            "artist": artists,
            "album": album,
            "duration": duration,
            "type": res.get("resultType"),
            "video_id": video_id,
            "browse_id": res.get("browseId"),  # albums/artists use this instead of videoId
            "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            "thumbnail": thumbnail_url,
        }

    @staticmethod
    def _format_single_result_summary(result: dict) -> str:
        """🌸 Plain string-formatted summary for exactly 1 result — no
        AI call at all, so there is zero chance of the model inventing
        a "mix"/"spread" of other artists that were never found (the
        production bug this replaces: a single Adele result summarized
        as "Adele and Rihanna... plus Ed Sheeran and Taylor Swift").
        Built directly from the result's own fields via string
        formatting/regex-safe interpolation — nothing here can drift
        from what was actually returned."""
        title = result.get("title") or "this track"
        artist = result.get("artist")
        result_type = (result.get("type") or "song").lower()

        if artist:
            return f'Found "{title}" by {artist}! 🎶'
        return f'Found "{title}"! 🎶' if result_type == "song" else f'Found the {result_type} "{title}"! 🎶'

    async def _summarize_with_groq(self, query: str, results: list) -> str:
        """🌸 Summary string for a set of search results.

        Exactly 1 result -> handled entirely by
        _format_single_result_summary (no AI call, see that method's
        docstring for why). Only 2+ results reach Groq below, since
        that's the only case where a genuine "spread" of artists/genres
        actually exists to describe — the model is never even given
        the chance to hallucinate one from a single item anymore.

        🌸 No post-hoc verification of the 2+ summary against `results`
        — a regex check against artist/title/album was tried and
        removed: real Groq output uses inconsistent whitespace (narrow
        no-break spaces like "Ed\u202fSheeran" instead of a normal
        space) and other formatting quirks that made a real, correct
        mention of an actual result look "unknown" and get rejected —
        a false positive throwing away a good summary is worse than an
        occasional true positive slipping through. MUSIC_SUMMARY_SYSTEM_PROMPT's
        instruction to only use artists/titles that appear in the list
        is trusted at face value here, same trust level every other
        classifier in this file (classify_music_intent,
        rewrite_search_query) already operates at.

        Falls back to a plain templated sentence (no AI) on ANY
        problem — no client, API error, timeout, malformed JSON — same
        degrades-quietly convention as classify_categories() in
        groq_exa_search.py, so a Groq hiccup never breaks the search
        results themselves."""
        fallback = (
            f"Found {len(results)} result(s) for \"{query}\"! 🎶"
            if results else f"Couldn't find anything for \"{query}\". 🥺"
        )
        if not results:
            return fallback

        if len(results) == 1:
            return self._format_single_result_summary(results[0])

        if not self.groq_client:
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
                # 🌸 reasoning_effort="low" + generous max_tokens — gpt-oss-20b
                # spends tokens on hidden chain-of-thought before content,
                # so a tight cap risks an empty response (see
                # MUSIC_INTENT_CLASSIFIER_MODEL comment above for what that
                # looked like in production logs).
                reasoning_effort="low",
                max_tokens=300,
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
        if not self.groq_client:
            music_logger.info("[classify_music_intent] skipped: no groq_client (see startup log)")
            return False
        if not prompt:
            return False

        def _call():
            return self.groq_client.chat.completions.create(
                model=MUSIC_INTENT_CLASSIFIER_MODEL,
                messages=[
                    {"role": "system", "content": MUSIC_INTENT_CLASSIFIER_POLICY},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                # 🌸 low effort + 100 tokens is plenty of headroom for
                # "yes"/"no" plus whatever brief reasoning gpt-oss-20b
                # insists on doing first — 16 was too tight and produced
                # raw='' (empty) every single time in production.
                reasoning_effort="low",
                max_tokens=100,
            )

        try:
            response = await asyncio.to_thread(_call)
            raw = (response.choices[0].message.content or "").strip().lower()
            result = raw.startswith("yes")
            if not raw:
                # 🌸 Empty content is the exact symptom that caused the
                # silent Aug 2026 outage (reasoning tokens ate the whole
                # budget before any content) — flag it loudly instead of
                # quietly logging alongside normal "no" results, so a
                # regression like this gets noticed fast next time.
                print("⚠️ Music intent classifier returned EMPTY content — check reasoning_effort/max_tokens")
            music_logger.info(f"[classify_music_intent] prompt={prompt!r} raw={raw!r} -> {result}")
            return result
        except Exception as e:
            print(f"⚠️ Music intent classifier error (defaulting to no): {e}")
            music_logger.info(f"[classify_music_intent] prompt={prompt!r} FAILED: {e}")
            return False

    async def decide_track_count(self, prompt: str, default: int = 5) -> int:
        """🌸 One cheap Groq call → how many tracks to return (1-20).
        Only called after classify_music_intent already said "yes", so
        this doesn't need its own zero-token gate. Fails safe to
        `default` on any problem — no client, API error, timeout, junk/
        out-of-range reply — same degrades-quietly convention as every
        other classifier in this file. Result is ALWAYS clamped to
        [1, MAX_SUGGESTED_TRACKS] regardless of what the model says."""
        if not self.groq_client or not prompt:
            return default

        def _call():
            return self.groq_client.chat.completions.create(
                model=TRACK_COUNT_MODEL,
                messages=[
                    {"role": "system", "content": TRACK_COUNT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                reasoning_effort="low",
                max_tokens=100,
            )

        try:
            response = await asyncio.to_thread(_call)
            raw = (response.choices[0].message.content or "").strip()
            raw = raw.strip("`").removeprefix("json").strip()
            parsed = json.loads(raw)
            count = int(parsed.get("count", default))
        except Exception as e:
            print(f"⚠️ Track count classifier error (using default={default}): {e}")
            music_logger.info(f"[decide_track_count] prompt={prompt!r} FAILED: {e}")
            return default

        # 🌸 Belt-and-suspenders clamp — never trust a model's number
        # blindly, even one it was explicitly told a ceiling for.
        count = max(1, min(count, MAX_SUGGESTED_TRACKS))
        music_logger.info(f"[decide_track_count] prompt={prompt!r} -> {count}")
        return count

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
                reasoning_effort="low",
                max_tokens=150,
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

        `limit` is clamped to [1, MAX_SUGGESTED_TRACKS] no matter what
        the caller passes — same final-safety-net convention as
        decide_track_count's own clamp, so a bad call site can never
        request more than the ceiling.

        On any failure (no ytm client, empty query, API error) returns
        the same shape with an empty results list and an apologetic
        summary, so callers never need a special-case branch — they can
        always just read result["results"] and result["summary"].
        """
        query = (query or "").strip()
        limit = max(1, min(int(limit or 1), MAX_SUGGESTED_TRACKS))
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

        # 🌸 ytmusicapi's own `limit` param is unreliable for some
        # filters (observed: filter="songs" returning ~20 results even
        # when limit=2 was passed) — it's a page-size hint to the
        # underlying API, not a hard cap. Slice ourselves right after
        # the call so `results` always matches what was actually
        # asked for, regardless of what ytmusicapi's API returns.
        raw_results = raw_results[:limit] if raw_results else raw_results
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
# used elsewhere (pink/purple gradient) and music.py's signature pink.
MUSIC_EMBED_COLOR = 0xFFB6C1

YTM_ICON_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/6/6a/"
    "Youtube_Music_icon.svg/1280px-Youtube_Music_icon.svg.png"
)

# 🌸 Per-server SQLite for interceptor interactions, mirroring the
# codebase's cache/guild_data/guild_<id>.db pattern in shared.py —
# same "own small db, own dir" convention, just scoped to this module's
# button interactions instead of guild snapshots.
INTERACTIONS_DB_DIR = "interactions"
INTERACTIONS_DB_PATH = os.path.join(INTERACTIONS_DB_DIR, "yt_music_interactions.db")
os.makedirs(INTERACTIONS_DB_DIR, exist_ok=True)

_INTERACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS music_interactions (
    message_id  INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER,
    query       TEXT NOT NULL,
    filter_type TEXT NOT NULL,
    tracks_json TEXT NOT NULL,
    track_index INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


def get_music_db_path(guild_id: int | None, channel_id: int | None) -> str:
    """🌸 Splits SQLite database files into guild and DM paths."""
    if guild_id:
        folder = os.path.join("interactions", "music", "guild", str(guild_id))
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"music_button_{guild_id}.db")
    else:
        cid = channel_id or 0
        folder = os.path.join("interactions", "music", "dm", str(cid))
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"music_button_{cid}.db")


async def _db_init(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_INTERACTIONS_SCHEMA)
        cursor = await db.execute("PRAGMA table_info(music_interactions);")
        existing_cols = {row[1] for row in await cursor.fetchall()}
        if "summary" not in existing_cols:
            await db.execute("ALTER TABLE music_interactions ADD COLUMN summary TEXT;")
        await db.commit()


async def save_music_interaction(message_id: int, user_id: int, guild_id: int | None,
                                  channel_id: int | None, query: str, filter_type: str,
                                  tracks: list, track_index: int = 0, summary: str | None = None):
    db_path = get_music_db_path(guild_id, channel_id)
    await _db_init(db_path)
    now = time.time()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO music_interactions
                (message_id, user_id, guild_id, query, filter_type, tracks_json, track_index, summary, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                track_index = excluded.track_index,
                summary     = excluded.summary,
                updated_at  = excluded.updated_at
            """,
            (message_id, user_id, guild_id, query, filter_type,
             json.dumps(tracks, ensure_ascii=False), track_index, summary, now, now),
        )
        await db.commit()


async def update_music_interaction_index(guild_id: int | None, channel_id: int | None,
                                         message_id: int, track_index: int):
    db_path = get_music_db_path(guild_id, channel_id)
    await _db_init(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE music_interactions SET track_index = ?, updated_at = ? WHERE message_id = ?",
            (track_index, time.time(), message_id),
        )
        await db.commit()


async def load_music_interaction(guild_id: int | None, channel_id: int | None,
                                  message_id: int) -> dict | None:
    db_path = get_music_db_path(guild_id, channel_id)
    await _db_init(db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM music_interactions WHERE message_id = ?", (message_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    data = dict(row)
    data["tracks_json"] = json.loads(data["tracks_json"])
    return data


def _track_urls(track: dict) -> tuple[str, str, str]:
    """🌸 Builds the same 3 URL shapes music.py's create_content
    derives from videoId — short/music/video — from our normalized
    track dict's `video_id` field."""
    video_id = track.get("video_id")
    short_url = f"https://youtu.be/{video_id}" if video_id else "https://youtube.com"
    music_url = f"https://music.youtube.com/watch?v={video_id}" if video_id else short_url
    video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else short_url
    return short_url, music_url, video_url

def _build_track_view(track: dict, index: int, total: int, query: str, summary: str | None = None):
    """🌸 Membuat kartu musik V2 dengan separator ganda estetis (atas & bawah ringkasan)."""
    title = track.get("title") or "Unknown Title"
    artist = track.get("artist") or "Unknown Artist"
    album = track.get("album") or "Single"
    duration = track.get("duration") or "N/A"
    track_type = (track.get("type") or "song").capitalize()
    short_url, _, _ = _track_urls(track)
    thumbnail = track.get("thumbnail") or MUSIC_THUMBNAIL_URL

    view = discord.ui.LayoutView(timeout=None)

    # 1. Informasi Baris Atas (Detail Lagu)
    top_text = (
        f"## 🎶 [{title}]({short_url})\n"
        f"**👤 Artist:** {artist}  |  **💿 Album:** {album}\n"
        f"**⏱️ Duration:** `{duration}`  |  **🎧 Type:** {track_type}"
    )

    # 2. Informasi Baris Metadata Hasil (Paling Bawah)
    meta_text = f"*Result {index + 1} of {total} for `{' '.join(query.split())}`*"

    # Inisialisasi Container Utama Kosong
    container = discord.ui.Container()

    # Masukkan Bagian Atas & Separator Pertama
    container.add_item(discord.ui.TextDisplay(top_text))
    container.add_item(discord.ui.Separator())

    # 🌸 Masukkan Ringkasan AI & Separator Kedua (Hanya jika ringkasan tersedia)
    if summary:
        container.add_item(discord.ui.TextDisplay(f"> 💬 {summary}"))
        container.add_item(discord.ui.Separator())  # Pembatas antara Summary dan Metadata

    # Masukkan Bagian Metadata & Thumbnail Gambar
    container.add_item(discord.ui.TextDisplay(meta_text))
    container.add_item(discord.ui.MediaGallery(
        discord.MediaGalleryItem(media=thumbnail, description="Artwork")
    ))

    view.add_item(container)
    return view

class MusicPaginatorView(discord.ui.LayoutView):
    """🌸 Paginator Components V2 dengan separator ganda pemisah ringkasan AI."""

    def __init__(self, owner_id: int | None = None, current_index: int = 0,
                 total_tracks: int = 1, track: dict | None = None,
                 query: str = "", summary: str | None = None):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.current_index = current_index
        self.total_tracks = total_tracks

        if track is None:
            track = {
                "title": "Music Search",
                "artist": "Ask me for a song 🎶",
                "album": "",
                "duration": "",
                "type": "song",
                "thumbnail": MUSIC_THUMBNAIL_URL,
            }

        title = track.get("title") or "Unknown Title"
        artist = track.get("artist") or "Unknown Artist"
        album = track.get("album") or "Single"
        duration = track.get("duration") or "N/A"
        track_type = (track.get("type") or "song").capitalize()
        short_url, _, _ = _track_urls(track)
        thumbnail = track.get("thumbnail") or MUSIC_THUMBNAIL_URL

        top_text = (
            f"## 🎶 [{title}]({short_url})\n"
            f"**👤 Artist:** {artist}  |  **💿 Album:** {album}\n"
            f"**⏱️ Duration:** `{duration}`  |  **🎧 Type:** {track_type}"
        )
        
        meta_text = f"*Result {current_index + 1} of {total_tracks} for `{' '.join(query.split())}`*"

        # Inisialisasi Container Utama Kosong
        self.container = discord.ui.Container()

        # Masukkan Bagian Atas & Separator Pertama
        self.container.add_item(discord.ui.TextDisplay(top_text))
        self.container.add_item(discord.ui.Separator())

        # 🌸 Masukkan Ringkasan AI & Separator Kedua (Hanya jika ringkasan tersedia)
        if summary:
            self.container.add_item(discord.ui.TextDisplay(f"> 💬 {summary}"))
            self.container.add_item(discord.ui.Separator())  # Pembatas antara Summary dan Metadata

        # Masukkan Bagian Metadata & Thumbnail Gambar
        self.container.add_item(discord.ui.TextDisplay(meta_text))
        self.container.add_item(discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=thumbnail, description="Artwork")
        ))
        
        self.add_item(self.container)

        # Row 0: Tombol navigasi
        self.prev_btn = discord.ui.Button(
            label="Prev", emoji="◀️", style=discord.ButtonStyle.secondary,
            custom_id="music:prev",
        )
        self.counter_btn = discord.ui.Button(
            label=f"{current_index + 1} / {total_tracks}", emoji="🎵",
            style=discord.ButtonStyle.primary, custom_id="music:counter",
            disabled=True,
        )
        self.next_btn = discord.ui.Button(
            label="Next", emoji="▶️", style=discord.ButtonStyle.secondary,
            custom_id="music:next",
        )
        self.prev_btn.callback = self._prev_click
        self.next_btn.callback = self._next_click

        self.nav_row = discord.ui.ActionRow(
            self.prev_btn, self.counter_btn, self.next_btn
        )
        self.add_item(self.nav_row)

        # Row 1: Link eksternal
        _, music_url, video_url = _track_urls(track)
        self.links_row = discord.ui.ActionRow(
            discord.ui.Button(label="YT Music", url=music_url, emoji="🎧"),
            discord.ui.Button(label="YouTube", url=video_url, emoji="📺"),
        )
        self.add_item(self.links_row)


    async def _step(self, interaction: discord.Interaction, delta: int):
        message_id = interaction.message.id
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        row = await load_music_interaction(guild_id, channel_id, message_id)
        if not row:
            return await interaction.response.send_message(
                "This search session has expired or wasn't found! 🥺 Try asking me again?",
                ephemeral=True,
            )

        if interaction.user.id != row["user_id"]:
            return await interaction.response.send_message(
                "This isn't your search! 😭🙏 But you can ask me for music too! 🙂✨",
                ephemeral=True,
            )

        tracks = row["tracks_json"]
        if not tracks:
            return await interaction.response.send_message(
                "There aren't any tracks in this search session. 🥺",
                ephemeral=True,
            )

        new_index = (row["track_index"] + delta) % len(tracks)
        await update_music_interaction_index(guild_id, channel_id, message_id, new_index)

        current_track = tracks[new_index]
        view = create_music_view(
            row["user_id"], new_index, len(tracks), current_track,
            row["query"], row.get("summary"),
        )
        await interaction.response.edit_message(view=view)

    async def _prev_click(self, interaction: discord.Interaction):
        await self._step(interaction, -1)

    async def _next_click(self, interaction: discord.Interaction):
        await self._step(interaction, 1)


def create_music_view(owner_id: int, current_index: int, total_tracks: int,
                      track: dict, query: str = "", summary: str | None = None) -> MusicPaginatorView:
    """🌸 Builds a complete Components V2 music response."""
    return MusicPaginatorView(
        owner_id=owner_id,
        current_index=current_index,
        total_tracks=total_tracks,
        track=track,
        query=query,
        summary=summary,
    )


def register_persistent_music_view(bot):
    """🌸 Registers the Components V2 paginator for messages across restarts."""
    if getattr(bot, "_music_view_registered", False):
        return
    bot.add_view(MusicPaginatorView())
    bot._music_view_registered = True
    print("🌸 Persistent Components V2 music paginator view registered!")


class MusicPaginatorCog(commands.Cog):
    """🌸 Registers the persistent Components V2 music paginator."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        register_persistent_music_view(self.bot)
        print("🌸 MusicPaginatorCog loaded — Components V2 persistent view active!")


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicPaginatorCog(bot))


def _build_music_view(result: dict, max_tracks: int = 5):
    """🌸 Turns a suggest() result into (view, tracks, summary).
    `max_tracks` slices the already-fetched result["results"] for the
    paginator/embed — normally this is a no-op since suggest() is now
    called with the same count as `limit`, but the slice stays as a
    defensive final cap (clamped again below) in case a caller ever
    passes a larger `result` than it asked the paginator to show."""
    max_tracks = max(1, min(int(max_tracks or 1), MAX_SUGGESTED_TRACKS))

    if not result["results"]:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## 🎶 Music Search"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(result["summary"]),
            discord.ui.TextDisplay(f"YouTube Music • `{result['query']}`"),
        ))
        return view, None, result["summary"]

    tracks = result["results"][:max_tracks]
    view = create_music_view(
        owner_id=0,
        current_index=0,
        total_tracks=len(tracks),
        track=tracks[0],
        query=result["query"],
        summary=result["summary"],
    )
    return view, tracks, result["summary"]


async def handle_music_request(message: "discord.Message", guild_id: int, shared):
    """🌸 Interceptor entry point, same call contract as
    handle_media_request(message, guild_id, shared) in groq_pexels.py —
    slot this into the SAME spot in groq_service.py's interceptor chain
    (handle_mention_reaction), right alongside handle_media_request.

    `guild_id` and `shared` aren't used yet (music search has no
    per-guild state to read), but are accepted so the call signature
    matches every other interceptor in the chain and it can be added
    as a drop-in without special-casing the call site.

    Returns a (discord.Embed, list[dict] | None, str, str | None) tuple
    on a hit — (embed, tracks, query, summary) — or None if this
    message doesn't look like a music request (or the classifier says
    no), matching every other interceptor's "None = try the next
    thing" contract.

    ⚠️ `tracks` is None for the "couldn't find it" apology embed
    (nothing to paginate) — `summary` is also None in that case, just
    send the embed plain. When `tracks` IS populated, attach the view
    directly — custom_id is a fixed literal string ("music:prev"/
    "music:next"), not message-id-encoded, so there's no send-then-edit
    dance:

        hit = await handle_music_request(message, guild_id, shared)
        if hit:
            embed, tracks, query, summary = hit
            if tracks:
                view = MusicPaginatorView(owner_id=message.author.id)
                _, music_url, video_url = _track_urls(tracks[0])
                view.add_item(discord.ui.Button(label="YT Music", url=music_url, emoji="🎧"))
                view.add_item(discord.ui.Button(label="YouTube", url=video_url, emoji="📲"))
                sent = await message.reply(embed=embed, view=view)
                await save_music_interaction(
                    message_id=sent.id,
                    user_id=message.author.id,
                    guild_id=guild_id,
                    query=query,
                    filter_type="songs",
                    tracks=tracks,
                    track_index=0,
                    summary=summary,
                )
            else:
                await message.reply(embed=embed)
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

    # 🌸 How many tracks to show — explicit user number ("give me 10
    # songs") wins, otherwise the AI picks a sensible amount based on
    # phrasing (single song vs "a few" vs "a playlist"). Always clamped
    # to [1, MAX_SUGGESTED_TRACKS] inside decide_track_count itself.
    track_count = await music_service.decide_track_count(text)

    # 🌸 Try the raw message as a literal ytmusicapi search first — the
    # common case (a real title/artist named) is handled here with no
    # extra cost. Only reach for Groq below if that comes up genuinely
    # empty, e.g. vague asks like "give me a cool song" that have
    # nothing searchable in them — and even then, Groq's rewrite gets
    # verified against ytm.search() again before it's ever shown to
    # the user, so a bad guess just means "couldn't find it" rather
    # than a hallucinated result.
    result = await music_service.suggest(text, limit=track_count)
    if not result["results"]:
        rewritten_query = await music_service.rewrite_search_query(text)
        if rewritten_query:
            result = await music_service.suggest(rewritten_query, limit=track_count)

    if not result["results"]:
        # 🌸 Once the classifier commits to "this IS a music request",
        # we must not let it silently fall through to the generic chat
        # model — that's how you get hallucinated song titles/links.
        # An honest "couldn't find it" beats a made-up track every time.
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## 🎶 Music Search"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f'Couldn\'t find anything for "{text}" 🥺 try naming the song/artist directly?'
            ),
        ))
        return view, None, text, None

    view, tracks, summary = _build_music_view(result, max_tracks=track_count)
    return view, tracks, result["query"], summary


# 🌸 Quick manual smoke test — run directly (`python groq_music_suggestion.py
# "some query"`) to sanity-check search + summary output without needing
# the bot running. Not imported/used by anything else.
if __name__ == "__main__":
    async def _main():
        service = MusicSuggestionService()
        q = " ".join(sys.argv[1:]) or "lofi hip hop"
        result = await service.suggest(q)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\n(embed/view build skipped in CLI mode — needs a discord.User)")

    asyncio.run(_main())
