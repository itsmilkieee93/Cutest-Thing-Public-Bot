# encoding: utf-8
"""
🌸 groq_pexels.py — regex-based Pexels interceptor for the Groq mention flow.

Sits alongside the other handle_*_query() interceptors in groq_instruct.py
(same call signature: message, guild_id, shared -> str | None) so it can be
dropped straight into bot_service.py's interceptor chain in
handle_mention_reaction().

Design:
  - Regex detects "send/show/find/get me a pic/photo/image of <query>" style
    phrasing directed at the bot (see PEXELS_REQUEST_PATTERN /
    PEXELS_VIDEO_REQUEST_PATTERN docstrings for both supported phrasings).
  - On match, hits the Pexels search API directly (bare API key auth, same
    as pexels.py) and grabs ONE result.
  - 45% of the time (CAPTION_CHANCE), a short cute one-liner from a quick,
    lightweight Groq call is prepended above the link. The rest of the
    time it's just the bare link.
  - Returns the raw Pexels CDN link (photo["src"]["large2x"] for photos, a
    direct .mp4 video_files link for videos) as a bare string — optionally
    with a caption line above it — no embed, no markdown link wrapper.
    Discord auto-unfurls any bare media URL into an embed/player on its
    own, so message.reply(...) is all that's needed. This keeps the base
    case token-free (bypasses Groq's full personality/memory pipeline
    entirely) and avoids double-embedding.
  - Returns None if the pattern doesn't match, the API key is missing, the
    request fails, or nothing is found — caller falls through to the next
    interceptor / Groq as normal.
"""

import os
import re
import sys
import random
import logging
import asyncio
import aiohttp
from groq import Groq

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

# ─── Logger ───────────────────────────────────────────────────────────────────
os.makedirs('log', exist_ok=True)
logger = logging.getLogger('GroqPexels')
logger.setLevel(logging.INFO)
_handler = logging.FileHandler('log/groq_pexels.log', encoding='utf-8')
_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
if not logger.handlers:
    logger.addHandler(_handler)

PEXELS_API_BASE = "https://api.pexels.com"

# 🌸 PEXELS PIC REQUEST DETECTION — two phrasings, both handled:
#   (A) "...pic/photo/image OF <query>"   e.g. "send me a pic of cats"
#   (B) "<query> pic/photo/image"         e.g. "find me a cat image",
#                                               "get me a dog pic"
# Named groups "q1"/"q2" hold whichever slot has the free-text subject.
# Phrasing (B)'s subject is deliberately restricted to 1-3 plain words
# (letters only) with a negative lookahead blocking "my/your/his/her/our/
# their" — this stops false-positives like "show me my profile picture"
# or "can you show me a picture" (no real subject) from misfiring, since
# those aren't Pexels search requests.
#   Matches: "send me a pic of cats", "show me a photo of the beach",
#            "find me a cat image", "get me a dog pic please"
#   Does NOT match: "show me my profile picture", "show me a picture"
PEXELS_REQUEST_PATTERN = re.compile(
    r"\b(?:send|show|find|get)\s+me\s+(?:an?\s+)?"
    r"(?:"
        r"(?:pic(?:ture)?s?|photos?|images?)\s+of\s+(?P<q1>.+)"
        r"|"
        r"(?!my\s|your\s|his\s|her\s|our\s|their\s|a\s+picture\b|a\s+pic\b|a\s+photo\b|an\s+image\b)"
        r"(?P<q2>[a-z]+(?:\s+[a-z]+){0,2})\s+(?:pic(?:ture)?s?|photos?|images?)\b"
    r")",
    re.IGNORECASE,
)

# 🌸 PEXELS VIDEO REQUEST DETECTION — same two-phrasing shape (and same
# false-positive guards) as PEXELS_REQUEST_PATTERN above but for
# "video/videos/clip/clips", kept as its own pattern so it never shadows
# (or gets shadowed by) the photo one.
#   Matches: "send me a video of waves", "find me a rain video",
#            "get me a dog clip please"
#   Does NOT match: "show me my recording video", "show me a video"
PEXELS_VIDEO_REQUEST_PATTERN = re.compile(
    r"\b(?:send|show|find|get)\s+me\s+(?:an?\s+)?"
    r"(?:"
        r"(?:videos?|clips?)\s+of\s+(?P<q1>.+)"
        r"|"
        r"(?!my\s|your\s|his\s|her\s|our\s|their\s|a\s+video\b|a\s+clip\b)"
        r"(?P<q2>[a-z]+(?:\s+[a-z]+){0,2})\s+(?:videos?|clips?)\b"
    r")",
    re.IGNORECASE,
)

# 🌸 Trailing filler stripped off the tail of the captured query so
# "cats please 🙏" or "dogs pls?" becomes just "cats" / "dogs".
_TRAILING_FILLER_PATTERN = re.compile(
    r"[\s,.!?]*\b(please|pls|plz|thanks?|ty)\b[\s,.!?]*$|[\s.!?,]+$",
    re.IGNORECASE,
)


def _get_headers() -> dict | None:
    """🌸 Pexels uses a bare API key — no Bearer or Client-ID prefix."""
    key = (getattr(key_config, "PEXELS_API_KEY", "") or "").strip()
    if key:
        return {
            "Authorization": key,
            "Accept": "application/json",
        }
    return None


def _clean_query(raw: str) -> str:
    q = _TRAILING_FILLER_PATTERN.sub("", raw).strip()
    # Strip a leading "a/an/the " the regex's (?:an?\s+) group didn't eat
    # (e.g. captured group started after "of the beach").
    q = re.sub(r"^(?:a|an|the)\s+", "", q, flags=re.IGNORECASE).strip()
    return q


def _extract_query(match: re.Match) -> str:
    """🌸 Pulls the matched subject out of whichever alternative fired —
    'q1' for the "...of <query>" phrasing, 'q2' for the "<query> pic/video"
    phrasing — then runs it through the same filler/article cleanup."""
    raw = match.group("q1") or match.group("q2") or ""
    return _clean_query(raw)


# 🌸 CAPTION CHANCE — 45% of the time, prepend a short Groq-generated
# kawaii one-liner above the bare CDN link instead of sending just the
# link on its own. Kept independent of GroqService's full personality /
# memory / safeguard pipeline on purpose — this is a cheap, throwaway,
# one-off flavor line, not a "real" conversational turn, so it shouldn't
# get logged into per-user Groq memory or cost a full context build.
CAPTION_CHANCE = 0.45

# 🌸 Small/fast model for the caption — doesn't need reasoning, just a
# quick cute line, so no need to pull from groq_ai.MODEL_POOL (that pool
# is tuned for full chat replies, some of it deprecated/preview-only).
CAPTION_MODEL = "llama-3.1-8b-instant"

CAPTION_SYSTEM_PROMPT = (
    "You are Cutest Thing, a kawaii-themed Discord bot with a pink/purple "
    "sparkly aesthetic. You just found a photo or video for someone. Write "
    "ONE short, cute, enthusiastic caption/reaction to go above the media "
    "link — under 12 words, casual, can use 1-2 emoji (🌸💖✨🎀). No "
    "quotation marks, no markdown, just the line itself. Don't describe "
    "the image in detail since you haven't actually seen it yet — just "
    "react with excitement about finding it."
)


def _get_groq_client() -> Groq | None:
    """🌸 Standalone lightweight Groq client for captions only — separate
    from GroqService's client so this module has no hard dependency on
    bot_service/groq_ai wiring and can be imported/tested on its own."""
    keys = list(getattr(key_config, "GROQ_API_KEYS", []) or [])
    if not keys:
        return None
    return Groq(api_key=keys[0])


async def _maybe_caption(query: str, media_url: str, kind: str) -> str:
    """🌸 With CAPTION_CHANCE probability, asks Groq for a short cute
    caption and prepends it above the media URL on its own line. Falls
    back to the bare URL (no caption) on any failure, missing key, or
    when the dice roll doesn't hit — never blocks/breaks the Pexels send.

    kind is "photo" or "video", just for the log line.
    """
    if random.random() >= CAPTION_CHANCE:
        return media_url

    client = _get_groq_client()
    if not client:
        return media_url

    def _call():
        return client.chat.completions.create(
            model=CAPTION_MODEL,
            messages=[
                {"role": "system", "content": CAPTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"You found a {kind} of: {query}"},
            ],
            temperature=0.9,
            max_tokens=30,
        )

    try:
        response = await asyncio.to_thread(_call)
        caption = (response.choices[0].message.content or "").strip()
        # 🌸 Strip stray wrapping quotes some models add despite instructions.
        caption = caption.strip('"').strip("'").strip()
        if not caption:
            return media_url
        return f"{caption}\n{media_url}"
    except Exception as e:
        logger.warning(f"Caption generation failed ({kind}, query='{query}'): {e}")
        return media_url


async def handle_pexels_photo_request(message, guild_id: int, shared) -> str | None:
    """
    🌸 REGEX PEXELS HANDLER — intercepts "send/show/find/get me a pic of X"
    before it hits Groq, fetches one matching photo from Pexels, and
    returns the raw CDN link so Discord auto-embeds it in the reply.

    Returns the bare image URL string on success, or None if the pattern
    doesn't match / nothing is found / the request fails (caller falls
    through to the next interceptor or Groq normally).
    """
    match = PEXELS_REQUEST_PATTERN.search(message.content)
    if not match:
        return None

    query = _extract_query(match)
    if not query:
        return None

    headers = _get_headers()
    if not headers:
        logger.warning("PEXELS_API_KEY missing — skipping Pexels intercept.")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            params = {
                "query":    query,
                "per_page": 1,
                "page":     random.randint(1, 50),
            }
            async with session.get(
                f"{PEXELS_API_BASE}/v1/search",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Pexels returned {resp.status} for query '{query}'")
                    return None
                data = await resp.json()

    except aiohttp.ClientError as e:
        logger.error(f"Connection error for query '{query}': {e}")
        return None

    try:
        photos = data.get("photos", [])
        if not photos:
            logger.info(f"No photos found for query '{query}'")
            return None

        photo = photos[0]
        image_url = photo["src"]["large2x"]  # 🌸 raw CDN link — Discord auto-embeds this
        photo_id = photo.get("id")

    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"Parse error for query '{query}': {e}")
        return None

    logger.info(
        f"📷 Intercepted pic request | User: {message.author} ({message.author.id}) "
        f"| Query: '{query}' | Photo ID: {photo_id} | Guild: {guild_id}"
    )

    return await _maybe_caption(query, image_url, "photo")


async def handle_pexels_video_request(message, guild_id: int, shared) -> str | None:
    """
    🌸 REGEX PEXELS VIDEO HANDLER — intercepts "send/show/find/get me a
    video of X" before it hits Groq, fetches one matching video from
    Pexels, and returns a direct .mp4 CDN link so Discord auto-embeds a
    playable video player in the reply.

    Returns the bare video file URL string on success, or None if the
    pattern doesn't match / nothing is found / the request fails (caller
    falls through to the next interceptor or Groq normally).
    """
    match = PEXELS_VIDEO_REQUEST_PATTERN.search(message.content)
    if not match:
        return None

    query = _extract_query(match)
    if not query:
        return None

    headers = _get_headers()
    if not headers:
        logger.warning("PEXELS_API_KEY missing — skipping Pexels video intercept.")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            params = {
                "query":    query,
                "per_page": 1,
                "page":     random.randint(1, 20),
            }
            async with session.get(
                f"{PEXELS_API_BASE}/videos/search",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Pexels videos returned {resp.status} for query '{query}'")
                    return None
                data = await resp.json()

    except aiohttp.ClientError as e:
        logger.error(f"Connection error for video query '{query}': {e}")
        return None

    try:
        videos = data.get("videos", [])
        if not videos:
            logger.info(f"No videos found for query '{query}'")
            return None

        vid = videos[0]
        vid_id = vid.get("id")

        # 🌸 Discord only auto-embeds a *direct* media file link (ending in
        # .mp4 etc.), not the Pexels page URL — so pick a real video_file,
        # preferring "sd" quality to keep the file small enough for Discord
        # to reliably unfurl/stream inline (hd files can be large + slow).
        files = vid.get("video_files", [])
        sd_files = [f for f in files if f.get("quality") == "sd"]
        hd_files = [f for f in files if f.get("quality") == "hd"]
        best_file = (sd_files or hd_files or files or [None])[0]

        if not best_file or not best_file.get("link"):
            logger.info(f"No usable video file found for query '{query}'")
            return None

        video_url = best_file["link"]  # 🌸 direct .mp4 CDN link — Discord auto-embeds this

    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"Parse error for video query '{query}': {e}")
        return None

    logger.info(
        f"🎬 Intercepted video request | User: {message.author} ({message.author.id}) "
        f"| Query: '{query}' | Video ID: {vid_id} | Guild: {guild_id}"
    )

    return await _maybe_caption(query, video_url, "video")
