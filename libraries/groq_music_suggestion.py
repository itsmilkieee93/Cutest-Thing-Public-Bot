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

# 🌸 Same fixed-cheap-model shape as MUSIC_INTENT_CLASSIFIER_MODEL — one
# extra Groq call, only ever spent once handle_music_request has ALREADY
# committed to "this is a music request" (see call order below), so this
# never costs anything on messages that aren't music requests at all.
MUSIC_PAGE_COUNT_MODEL = "openai/gpt-oss-20b"

MUSIC_PAGE_COUNT_SYSTEM_PROMPT = (
    "A Discord user is asking for music. Decide how many song results "
    "they most likely want, from 1 to 30.\n"
    "Rules of thumb:\n"
    "- A single specific song/artist named, or a vague 'give me a "
    "song' with no plural/quantity hint -> 1.\n"
    "- Plural wording with no number ('some songs', 'a few lofi "
    "beats', 'recommend me some tracks') -> 5-8.\n"
    "- An explicit number in the message -> use that number, capped "
    "at 30.\n"
    "- Broad/open asks ('give me a ton of songs', 'load me up with "
    "music', 'all their songs') -> 20-30.\n"
    "Respond with ONLY a single-line JSON object, nothing else, no "
    "markdown fences, in exactly this shape:\n"
    '{"count": <integer 1-30>}'
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

    async def classify_page_count(self, prompt: str) -> int:
        """🌸 One cheap Groq call → how many result pages (1-30) this
        request most likely wants, same fail-safe shape as
        classify_music_intent above. Fails to 8 (the previous fixed
        default) on any problem — no client, API error, junk reply —
        so a classifier hiccup just means "same behavior as before
        this feature existed" instead of an unpredictable page count."""
        DEFAULT_COUNT = 8
        if not self.groq_client or not prompt:
            return DEFAULT_COUNT

        def _call():
            return self.groq_client.chat.completions.create(
                model=MUSIC_PAGE_COUNT_MODEL,
                messages=[
                    {"role": "system", "content": MUSIC_PAGE_COUNT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                # 🌸 Same reasoning-model headroom fix as every other
                # gpt-oss-20b call in this file — see
                # MUSIC_INTENT_CLASSIFIER_MODEL's comment for the exact
                # empty-content failure this avoids.
                reasoning_effort="low",
                max_tokens=150,
            )

        try:
            response = await asyncio.to_thread(_call)
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                print("⚠️ Page-count classifier returned EMPTY content — check reasoning_effort/max_tokens")
                return DEFAULT_COUNT
            # 🌸 Defensive strip: some models wrap JSON in ```json fences
            # even when told not to.
            raw = raw.strip("`").removeprefix("json").strip()
            parsed = json.loads(raw)
            count = int(parsed.get("count", DEFAULT_COUNT))
            count = max(1, min(30, count))  # 🌸 hard clamp, 30 max as requested
            music_logger.info(f"[classify_page_count] prompt={prompt!r} -> {count}")
            return count
        except Exception as e:
            print(f"⚠️ Page-count classifier error (defaulting to {DEFAULT_COUNT}): {e}")
            music_logger.info(f"[classify_page_count] prompt={prompt!r} FAILED: {e}")
            return DEFAULT_COUNT

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
INTERACTIONS_DB_PATH = os.path.join(INTERACTIONS_DB_DIR, "interactions.db")
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


async def _db_init():
    """🌸 Creates the table on first use — cheap no-op after that,
    same lazy-init shape as load_guild_db in shared.py.

    Also runs a one-time ALTER TABLE migration for the `summary`
    column — CREATE TABLE IF NOT EXISTS only applies to a table that
    doesn't exist yet, so anyone with an existing interactions.db from
    before this column was added needs it added on top, or every
    save/load below (which now reads/writes `summary`) breaks with
    "no such column" on their already-running bot.
    """
    async with aiosqlite.connect(INTERACTIONS_DB_PATH) as db:
        await db.execute(_INTERACTIONS_SCHEMA)
        cursor = await db.execute("PRAGMA table_info(music_interactions);")
        existing_cols = {row[1] for row in await cursor.fetchall()}
        if "summary" not in existing_cols:
            await db.execute("ALTER TABLE music_interactions ADD COLUMN summary TEXT;")
        await db.commit()


async def save_music_interaction(message_id: int, user_id: int, guild_id: int | None,
                                  query: str, filter_type: str, tracks: list,
                                  track_index: int = 0, summary: str | None = None):
    """🌸 Upserts one row keyed by the sent message's ID, so a button
    press later just needs the message_id to reload full track state —
    no in-memory dict needed, survives bot restarts (Termux/mobile
    process gets killed a lot, same reasoning as the SQLite guild cache
    migration).

    `summary` is the AI-written blurb shown on the FIRST track's embed
    (see handle_music_request) — persisted here too so ◀️/▶️ presses
    can keep showing it on every page instead of only on the message
    as it was originally sent.
    """
    await _db_init()
    now = time.time()
    async with aiosqlite.connect(INTERACTIONS_DB_PATH) as db:
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


async def update_music_interaction_index(message_id: int, track_index: int):
    """🌸 Called on every ◀️/▶️ press so track_index survives a bot
    restart mid-pagination — matches the button handler's in-memory
    self.index, just persisted alongside it."""
    await _db_init()
    async with aiosqlite.connect(INTERACTIONS_DB_PATH) as db:
        await db.execute(
            "UPDATE music_interactions SET track_index = ?, updated_at = ? WHERE message_id = ?",
            (track_index, time.time(), message_id),
        )
        await db.commit()


async def load_music_interaction(message_id: int) -> dict | None:
    """🌸 Fetches a saved interaction row by message_id, e.g. to
    rehydrate a MusicPaginatorView after a restart. Returns None if
    unknown, same not-found convention as load_guild_cache."""
    await _db_init()
    async with aiosqlite.connect(INTERACTIONS_DB_PATH) as db:
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


def _build_track_embed(track: dict, index: int, total: int, query: str, summary: str | None = None) -> discord.Embed:
    """🌸 Album-art-forward embed for ONE track, styled after
    YTMPaginator.create_content in music.py (title/album/artist/
    duration + big thumbnail + footer with position), minus the extra
    live view/like-count API calls — those need a separate YouTube API
    key + socialcounts.org round trip that this lighter interceptor
    embed doesn't need to make on every mention hit."""
    title = track.get("title") or "Unknown Title"
    artist = track.get("artist") or "Unknown Artist"
    album = track.get("album") or "Single"
    duration = track.get("duration") or "N/A"
    short_url, _, _ = _track_urls(track)

    lines = []
    if summary:
        lines.append(summary + "\n")
    lines.append(f"**🏞 Album:** {album}")
    lines.append(f"**👤 Artist:** {artist}")
    lines.append(f"**⏱️ Duration:** {duration}")
    lines.append(f"\n**🔗 Link:** [Open in Browser]({short_url}) ✨")

    embed = discord.Embed(
        title=f"🎶 {title}",
        description="\n".join(lines),
        color=MUSIC_EMBED_COLOR,
    )

    thumb = track.get("thumbnail")
    embed.set_thumbnail(url=thumb or MUSIC_THUMBNAIL_URL)

    embed.set_footer(
        text=f"YouTube Music • \"{query}\" • Result {index + 1} of {total} ✨",
        icon_url=YTM_ICON_URL,
    )
    return embed


class MusicPaginatorView(discord.ui.View):
    """🌸 Persistent version of music.py's YTMPaginator — survives bot
    restarts (important on Termux where the process gets killed a
    lot). Two things make a view "persistent" in discord.py:

      1. timeout=None — a timed-out view stops accepting interactions
         even if the bot is still registered for it.
      2. Every component needs a FIXED, STABLE custom_id — the exact
         same string on every message this view ever produces.
         discord.py's persistent-view dispatch matches on the literal
         custom_id string of whatever was passed to bot.add_view(); it
         does NOT do any per-message or wildcard matching. Baking the
         message_id into the custom_id (the old "music_prev:<id>"
         scheme) meant every real message had a UNIQUE custom_id that
         never matched the single generic "music_prev:pending"
         registration from on_ready — so buttons only ever worked
         while the exact same in-memory instance/process was still
         alive, then went dead and Discord showed "The application
         didn't respond in time" on every click after a restart/reload.

    Instead: custom_id is the same literal string always
    ("music:prev" / "music:next"), and which MESSAGE is being paged is
    read straight off interaction.message.id inside the callback —
    Discord always provides that for free, no need to encode it
    ourselves. Track list, query, and current index still aren't kept
    on the instance; every press reloads them fresh from
    interactions.db via load_music_interaction(interaction.message.id).
    """

    def __init__(self, owner_id: int | None = None):
        super().__init__(timeout=None)  # 🎀 persistent — no expiry
        self.owner_id = owner_id

    async def _step(self, interaction: discord.Interaction, delta: int):
        # 🌸 message_id comes straight from Discord's interaction
        # payload — no parsing needed, works identically whether this
        # is the instance that sent the message or a freshly
        # registered generic instance after a restart.
        message_id = interaction.message.id
        row = await load_music_interaction(message_id)
        if not row:
            return await interaction.response.send_message(
                "This search has expired, sorry! 🥺 try asking me again?", ephemeral=True
            )

        # 🌸 Ownership check now reads user_id from the db row instead
        # of a stored self.user — same "not your search" UX, just
        # sourced from persisted state.
        if interaction.user.id != row["user_id"]:
            return await interaction.response.send_message(
                "This isn't your search! 😭🙏 But you can ask me for music too! 🙂✨️", ephemeral=True
            )

        tracks = row["tracks_json"]
        new_index = (row["track_index"] + delta) % len(tracks)
        await update_music_interaction_index(message_id, new_index)

        embed = _build_track_embed(tracks[new_index], new_index, len(tracks), row["query"], row.get("summary"))
        _, music_url, video_url = _track_urls(tracks[new_index])

        view = MusicPaginatorView(owner_id=row["user_id"])
        view.add_item(discord.ui.Button(label="YT Music", url=music_url, emoji="🎧"))
        view.add_item(discord.ui.Button(label="YouTube", url=video_url, emoji="📲"))

        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray, custom_id="music:prev")
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._step(interaction, -1)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray, custom_id="music:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._step(interaction, 1)


def register_persistent_music_view(bot):
    """🌸 Registers the persistent view so buttons on OLD messages
    (sent before a restart) keep working — Discord still shows them,
    but without this the client isn't listening for "music:prev" /
    "music:next" anymore and clicking does nothing. Since those are
    now fixed literal custom_ids (not per-message), one registration
    genuinely covers every message this view has ever produced or
    ever will. Guard against double-registration the same way
    _initial_guild_sync_done guards sync_all_guilds() in
    GroqMentionService — this can run more than once per process.

    Called automatically from MusicPaginatorCog.cog_load() below, so
    nothing needs to be hand-wired into bot_service.py's on_ready —
    same self-contained pattern wikipedia.py uses (its comment even
    says "registered once in cog_load", not on_ready). This is kept
    as a standalone function too in case a caller wants to invoke it
    directly, e.g. from a different cog's cog_load.
    """
    if getattr(bot, "_music_view_registered", False):
        return
    bot.add_view(MusicPaginatorView())
    bot._music_view_registered = True
    print("🌸 Persistent music paginator view registered!")


class MusicPaginatorCog(commands.Cog):
    """🌸 Tiny, self-contained cog whose ONLY job is registering
    MusicPaginatorView as persistent — same shape as wikipedia.py's
    cog, so this module doesn't depend on anyone remembering to add a
    line to bot_service.py's on_ready. cog_load() fires reliably on
    initial load AND every hot-reload, unlike on_ready which can be
    skipped entirely if this module is never imported from the file
    that owns on_ready, or can fire multiple times per process after a
    gateway reconnect — cog_load has neither problem.

    Load this like any other cog, e.g. in your extension list:
        "groq_music_suggestion"
    or manually:
        await bot.load_extension("groq_music_suggestion")
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        register_persistent_music_view(self.bot)
        print("🌸 MusicPaginatorCog loaded — persistent view active!")


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicPaginatorCog(bot))



def _build_music_embed(result: dict):
    """🌸 Turns a suggest() result dict into (embed, tracks, summary) —
    album art embed for track #0, the trimmed track list the caller
    needs to build a MusicPaginatorView, and the AI summary string (so
    it can be persisted via save_music_interaction and re-shown on
    every page, not just the first). Button-building happens at the
    call site (see handle_music_request's docstring) because a
    persistent view's custom_id has to encode the REAL message_id,
    which only exists after message.reply() returns — can't be baked
    in here.

    NOTE: the empty-results branch below is currently unreachable in
    practice — handle_music_request already checks
    `if not result["results"]` and returns its own apology embed
    BEFORE ever calling this function. Kept as a defensive fallback
    (and now a correctly-shaped 3-tuple) in case this is ever called
    directly from somewhere that skips that pre-check.
    """
    if not result["results"]:
        embed = discord.Embed(
            title="🎶 Music Search",
            description=result["summary"],
            color=MUSIC_EMBED_COLOR,
        )
        embed.set_thumbnail(url=MUSIC_THUMBNAIL_URL)
        embed.set_footer(text=f"YouTube Music • \"{result['query']}\"")
        return embed, None, result["summary"]

    tracks = result["results"][:30]  # 🌸 hard safety cap — matches classify_page_count's clamp;
                                       # already ≤30 from suggest(limit=page_count), this just
                                       # guards against a raw ytmusicapi response somehow
                                       # coming back longer than what was actually requested.
    embed = _build_track_embed(tracks[0], 0, len(tracks), result["query"], result["summary"])
    return embed, tracks, result["summary"]


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

    # 🌸 AI decides how many result pages this ask wants (1-30) —
    # only spent once we already know this IS a music request, so it
    # never adds cost to non-music messages. Defaults to 8 (the old
    # fixed cap) on any classifier problem — see classify_page_count.
    page_count = await music_service.classify_page_count(text)

    # 🌸 Try the raw message as a literal ytmusicapi search first — the
    # common case (a real title/artist named) is handled here with no
    # extra cost. Only reach for Groq below if that comes up genuinely
    # empty, e.g. vague asks like "give me a cool song" that have
    # nothing searchable in them — and even then, Groq's rewrite gets
    # verified against ytm.search() again before it's ever shown to
    # the user, so a bad guess just means "couldn't find it" rather
    # than a hallucinated result.
    result = await music_service.suggest(text, limit=page_count)
    if not result["results"]:
        rewritten_query = await music_service.rewrite_search_query(text)
        if rewritten_query:
            result = await music_service.suggest(rewritten_query, limit=page_count)

    if not result["results"]:
        # 🌸 Once the classifier commits to "this IS a music request",
        # we must not let it silently fall through to the generic chat
        # model — that's how you get hallucinated song titles/links.
        # An honest "couldn't find it" beats a made-up track every time.
        embed = discord.Embed(
            title="🎶 Music Search",
            description=f"Couldn't find anything for \"{text}\" 🥺 try naming the song/artist directly?",
            color=MUSIC_EMBED_COLOR,
        )
        return embed, None, text, None

    embed, tracks, summary = _build_music_embed(result)
    return embed, tracks, result["query"], summary


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
