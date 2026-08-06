# encoding: utf-8
"""
🌸 groq_pexels.py — AI-classified Pexels/Pixabay interceptor for the Groq
mention flow.

Sits alongside the other handle_*_query() interceptors in groq_instruct.py
(same call signature: message, guild_id, shared -> str | None) so it can be
dropped straight into bot_service.py's interceptor chain in
handle_mention_reaction().

Design:
  - This only ever runs on messages that already mention the bot / are DMs
    (handle_mention_reaction is only called from that gated path in
    bot_service.py's on_message), so a small classification call per
    message here is a bounded cost, not a per-message tax on the channel.
  - A SINGLE lightweight Groq call (_classify_media_request) replaces what
    used to be five separate regex patterns. It reads the message and
    returns structured JSON: whether this is a media request at all, what
    kind (photo / video / vector / cartoon), and a clean search query —
    detection, routing, and query extraction/refinement all in one pass.
    This is far more robust than regex against real phrasing variety
    ("draw me a...", "I want a picture showing...", "got any vids of...")
    without needing a new hand-written pattern for every phrasing.
  - Based on the classification, one handler hits the matching API:
      * photo  -> Pexels /v1/search
      * video  -> Pexels /videos/search
      * vector -> Pixabay ?image_type=vector
      * cartoon (illustration/clipart/drawing/sketch/animated) ->
        Pixabay ?image_type=illustration
  - 45% of the time (CAPTION_CHANCE), a short cute one-liner from a second
    quick Groq call becomes the embed's description (images) or is
    prepended above the bare link (video — Discord embeds can't play
    video via set_image, so video replies stay a plain text + link
    message like before). The rest of the time it's just the bare image/
    embed or bare video link, no caption.
  - Photo/vector/cartoon results are returned as a discord.Embed with the
    image URL set via set_image() — this renders the image cleanly with
    NO raw link text visible in the message (unlike a bare-URL reply,
    where Discord shows the link text above its own auto-generated
    embed). Video results are returned as a plain string (bare .mp4 CDN
    link, optionally with a caption line above it) since Discord embeds
    don't support playable video — only a raw link/attachment triggers
    Discord's native video player.
  - Pexels: photo["src"]["large2x"] for photos, a direct .mp4
    video_files link for videos. Pixabay: hit["webformatURL"], falling
    back to hit["largeImageURL"]/hit["vectorURL"].
  - Returns None if classification says "not a media request", the
    relevant API key is missing, the request fails, or nothing is found —
    caller falls through to the next interceptor / Groq's full reply as
    normal. Otherwise returns EITHER a discord.Embed (photo/vector/
    cartoon) or a plain str (video) — bot_service.py's reply site must
    branch on isinstance(intercepted, discord.Embed) to know whether to
    call message.reply(embed=...) or message.reply(content=...).

Pixabay notes:
  - Auth is a `key` query param, NOT a header (different from Pexels).
  - image_type accepts "photo", "illustration", "vector", "all".
  - Hotlinking Pixabay URLs long-term isn't allowed per their ToS, but
    "temporarily displaying search results" is explicitly permitted —
    which is exactly what an ephemeral Discord embed is, so this is fine.
  - webformatURL (max 640px) is available on the free tier and is
    Pixabay's own recommended size for temporary/hotlinked display — it's
    preferred here for embed reliability. vectorURL / fullHDURL /
    imageURL require an approved "full API access" account and are only
    used as a fallback if webformatURL is somehow missing from a hit.
"""

import os
import re
import sys
import json
import random
import logging
import asyncio
import aiohttp
import discord
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
PIXABAY_API_BASE = "https://pixabay.com/api"

# 🌸 Kinds the classifier is allowed to return. "vector" and "cartoon" both
# route to Pixabay (different image_type), "photo"/"video" route to Pexels.
VALID_KINDS = {"photo", "video", "vector", "cartoon"}


# ── Shared Groq client ─────────────────────────────────────────────────────

def _get_groq_client() -> Groq | None:
    """🌸 Standalone lightweight Groq client used for classification,
    query polish, and captions — separate from GroqService's client so
    this module has no hard dependency on bot_service/groq_ai wiring and
    can be imported/tested on its own."""
    keys = list(getattr(key_config, "GROQ_API_KEYS", []) or [])
    if not keys:
        return None
    return Groq(api_key=keys[0])


# ── AI classification (replaces the old regex patterns) ───────────────────

# 🌸 Fast/small model — this call needs to be quick since it gates every
# mention, but still sharp enough for reliable JSON + intent judgment.
CLASSIFY_MODEL = "llama-3.1-8b-instant"

CLASSIFY_SYSTEM_PROMPT = (
    "You are a strict intent classifier for a Discord bot's media-fetch "
    "feature. Decide whether the user's message is a direct request to "
    "be SENT a photo, video, vector graphic, or cartoon/illustration — "
    "as opposed to normal conversation, a question, or a request for "
    "something else (like generating original art, editing an image, "
    "or talking about a photo they already sent).\n\n"
    "Respond with ONLY a JSON object, no other text, matching exactly "
    "this shape:\n"
    '{"is_media_request": true or false, '
    '"kind": "photo" | "video" | "vector" | "cartoon" | null, '
    '"query": "short plain search phrase, 1-4 words, or null"}\n\n'
    "Rules:\n"
    "- is_media_request is true ONLY for clear asks like 'send me a "
    "picture of X', 'find me a video of X', 'show me a cartoon of X', "
    "'got any vids of X', 'draw— I mean find me a vector of X'.\n"
    "- kind is 'vector' for vector/vector-art/vector-graphic requests; "
    "'cartoon' for cartoon/clipart/illustration/drawing/sketch/animated-"
    "style requests; 'video'/'clip' wording -> 'video'; otherwise "
    "'photo' is the default for real-photo requests.\n"
    "- query is the bare subject only — no style words, no filler like "
    "'please'/'pls', no leading articles ('a'/'an'/'the'), lowercase.\n"
    "- If the message is NOT a media request (e.g. it's a question, "
    "small talk, or asks the bot to draw/generate/edit an image itself "
    "rather than fetch a stock one), set is_media_request to false and "
    "kind/query to null.\n"
    "- Never include commentary, markdown, or text outside the JSON "
    "object."
)


async def _classify_media_request(content: str) -> dict | None:
    """🌸 Single Groq call that replaces the old regex detection layer.
    Returns a dict like {"is_media_request": bool, "kind": str|None,
    "query": str|None}, or None on any failure (missing key, bad JSON,
    network error) — callers treat None the same as "not a media
    request" and fall through to the next interceptor / Groq normally.
    """
    client = _get_groq_client()
    if not client:
        return None

    def _call():
        return client.chat.completions.create(
            model=CLASSIFY_MODEL,
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            temperature=0.1,
            max_tokens=80,
            response_format={"type": "json_object"},
        )

    try:
        response = await asyncio.to_thread(_call)
        raw = (response.choices[0].message.content or "").strip()
        result = json.loads(raw)
    except Exception as e:
        logger.warning(f"Media request classification failed: {e}")
        return None

    if not isinstance(result, dict):
        return None

    is_request = bool(result.get("is_media_request"))
    kind = result.get("kind")
    query = result.get("query")

    if not is_request:
        return {"is_media_request": False, "kind": None, "query": None}

    if kind not in VALID_KINDS:
        kind = "photo"

    if not isinstance(query, str) or not query.strip():
        return {"is_media_request": False, "kind": None, "query": None}

    # 🌸 Defensive cleanup even though the prompt already asks for this —
    # models don't always perfectly follow formatting instructions.
    query = query.strip().strip('"').strip("'").lower()
    query = re.sub(r"^(?:a|an|the)\s+", "", query).strip()
    if not query or len(query.split()) > 6:
        return {"is_media_request": False, "kind": None, "query": None}

    return {"is_media_request": True, "kind": kind, "query": query}


def _resolve_pixabay_image_type(kind: str) -> str:
    return "vector" if kind == "vector" else "illustration"


# ── Caption generation (unchanged behavior, kept independent of the full
#    GroqService personality/memory pipeline — cheap throwaway flavor) ────

CAPTION_CHANCE = 0.45
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


async def _maybe_caption_text(query: str, kind: str) -> str | None:
    """🌸 With CAPTION_CHANCE probability, asks Groq for a short cute
    caption. Returns the caption string, or None if the dice roll didn't
    hit / Groq call failed / result was empty — callers decide what to
    do with a None (image path puts it in the embed description if
    present; video path prepends it above the bare link if present).

    kind is used only for the log line / prompt phrasing.
    """
    if random.random() >= CAPTION_CHANCE:
        return None

    client = _get_groq_client()
    if not client:
        return None

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
        caption = caption.strip('"').strip("'").strip()
        return caption or None
    except Exception as e:
        logger.warning(f"Caption generation failed ({kind}, query='{query}'): {e}")
        return None


async def _build_image_embed(query: str, image_url: str, kind: str) -> discord.Embed:
    """🌸 Builds a discord.Embed with the image set via set_image() — this
    is what actually hides the raw link text; a bare-URL reply always
    shows the link above Discord's own auto-embed, but embed.set_image()
    renders the image with nothing else visible. The optional cute
    caption (per CAPTION_CHANCE) becomes the embed description instead
    of a prepended text line.
    """
    caption = await _maybe_caption_text(query, kind)
    embed = discord.Embed(
        description=caption if caption else None,
        color=0xFFB6D9,  # 🌸 soft pink, matches the kawaii branding
    )
    embed.set_image(url=image_url)
    return embed


async def _maybe_caption_video_text(query: str, video_url: str) -> str:
    """🌸 Video results can't use discord.Embed.set_image() (Discord
    embeds don't support a playable video field the way set_image works
    for stills) — only a raw link or file attachment triggers Discord's
    native video player. So video replies stay a plain string: optional
    cute caption line above the bare .mp4 CDN link, same as before.
    """
    caption = await _maybe_caption_text(query, "video")
    if not caption:
        return video_url
    return f"{caption}\n{video_url}"


# ── Auth helpers ────────────────────────────────────────────────────────

def _get_pexels_headers() -> dict | None:
    """🌸 Pexels uses a bare API key — no Bearer or Client-ID prefix."""
    key = (getattr(key_config, "PEXELS_API_KEY", "") or "").strip()
    if key:
        return {
            "Authorization": key,
            "Accept": "application/json",
        }
    return None


def _get_pixabay_key() -> str | None:
    """🌸 Pixabay auth is a `key` query param, not a header."""
    key = (getattr(key_config, "PIXABAY_API_KEY", "") or "").strip()
    return key or None


# ── Single entry point: classify once, dispatch to the right API ──────────

async def handle_media_request(message, guild_id: int, shared) -> discord.Embed | str | None:
    """
    🌸 AI-CLASSIFIED MEDIA HANDLER — the single interceptor entry point.
    Replaces the old handle_pexels_photo_request / handle_pexels_video_
    request / handle_pixabay_style_request trio with one classifier call
    that decides intent + kind + query, then dispatches to Pexels (photo/
    video) or Pixabay (vector/cartoon).

    Returns a discord.Embed (photo/vector/cartoon — image set via
    set_image(), no visible link text) on success for image kinds, OR a
    plain str (bare .mp4 CDN link, optionally with a caption line above
    it — Discord embeds can't play video) for video kind, OR None if the
    message isn't a media request / the relevant API key is missing /
    the request fails / nothing is found — caller falls through to the
    next interceptor or Groq's full reply normally.

    bot_service.py's reply site must check isinstance(result,
    discord.Embed) to know whether to call message.reply(embed=...) or
    message.reply(content=...).
    """
    classification = await _classify_media_request(message.content)
    if not classification or not classification["is_media_request"]:
        return None

    kind = classification["kind"]
    query = classification["query"]

    if kind == "video":
        return await _fetch_pexels_video(message, guild_id, query)
    elif kind in ("vector", "cartoon"):
        return await _fetch_pixabay_image(message, guild_id, query, kind)
    else:  # "photo"
        return await _fetch_pexels_photo(message, guild_id, query)


async def _fetch_pexels_photo(message, guild_id: int, query: str) -> discord.Embed | None:
    """🌸 Hits Pexels /v1/search for a real photo and returns a
    discord.Embed with the image set via set_image(), or None on any
    failure/empty result."""
    headers = _get_pexels_headers()
    if not headers:
        logger.warning("PEXELS_API_KEY missing — skipping Pexels intercept.")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            params = {
                "query":       query,
                "orientation": "landscape",  # 🌸 avoid tall portrait images
                                              # dominating the whole screen
                                              # in a Discord embed (set_image
                                              # doesn't crop/resize — it
                                              # renders at native aspect
                                              # ratio, so a 3000x4000 portrait
                                              # photo pushes everything else
                                              # off-screen on mobile).
                "per_page":    1,
                "page":        random.randint(1, 50),
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
        logger.error(f"Connection error for Pexels photo query '{query}': {e}")
        return None

    try:
        photos = data.get("photos", [])
        if not photos:
            logger.info(f"No Pexels photos found for query '{query}'")
            return None
        photo = photos[0]
        image_url = photo["src"]["large2x"]
        photo_id = photo.get("id")
    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"Parse error for Pexels photo query '{query}': {e}")
        return None

    logger.info(
        f"📷 Media request (photo) | User: {message.author} ({message.author.id}) "
        f"| Query: '{query}' | Photo ID: {photo_id} | Guild: {guild_id}"
    )
    return await _build_image_embed(query, image_url, "photo")


async def _fetch_pexels_video(message, guild_id: int, query: str) -> str | None:
    """🌸 Hits Pexels /videos/search and returns a direct .mp4 CDN link
    (Discord only auto-embeds direct media files, not page URLs), or
    None on any failure/empty result."""
    headers = _get_pexels_headers()
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
        logger.error(f"Connection error for Pexels video query '{query}': {e}")
        return None

    try:
        videos = data.get("videos", [])
        if not videos:
            logger.info(f"No Pexels videos found for query '{query}'")
            return None
        vid = videos[0]
        vid_id = vid.get("id")

        # 🌸 Prefer "sd" quality — small enough for Discord to reliably
        # unfurl/stream inline (hd files can be large + slow to embed).
        files = vid.get("video_files", [])
        sd_files = [f for f in files if f.get("quality") == "sd"]
        hd_files = [f for f in files if f.get("quality") == "hd"]
        best_file = (sd_files or hd_files or files or [None])[0]

        if not best_file or not best_file.get("link"):
            logger.info(f"No usable video file found for query '{query}'")
            return None

        video_url = best_file["link"]
    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"Parse error for Pexels video query '{query}': {e}")
        return None

    logger.info(
        f"🎬 Media request (video) | User: {message.author} ({message.author.id}) "
        f"| Query: '{query}' | Video ID: {vid_id} | Guild: {guild_id}"
    )
    return await _maybe_caption_video_text(query, video_url)


async def _fetch_pixabay_image(message, guild_id: int, query: str, kind: str) -> discord.Embed | None:
    """🌸 Hits Pixabay's image search with image_type set from `kind`
    ("vector" -> vector, "cartoon" -> illustration) and returns a
    discord.Embed with the best available CDN link set via set_image(),
    or None on any failure/empty result."""
    api_key = _get_pixabay_key()
    if not api_key:
        logger.warning("PIXABAY_API_KEY missing — skipping Pixabay intercept.")
        return None

    image_type = _resolve_pixabay_image_type(kind)
    per_page = 10  # 🌸 kept >1 so the aspect-ratio picker below has a
                   # batch to choose from — see comment further down.

    base_params = {
        "key":         api_key,
        "q":           query,
        "image_type":  image_type,
        "safesearch":  "true",
        # 🌸 Randomize sort order so repeated requests for the same
        # query don't always surface the same top hits — Pixabay only
        # accepts "popular" or "latest" here (no third mode).
        "order":       random.choice(["popular", "latest"]),
        "per_page":    per_page,
    }

    try:
        async with aiohttp.ClientSession() as session:
            # 🌸 Always start on page=1 — it's guaranteed valid no matter
            # how few results this specific query has. This also hands us
            # totalHits, which is what actually gates how far we're
            # allowed to page: Pixabay 400s once page * per_page exceeds
            # a QUERY'S OWN totalHits (e.g. 'anime image' may only have
            # ~80 hits), not just the global 500-hit cap. A flat
            # randint(1, 50) assumed every query had the full 500 and
            # blew past that per-query ceiling for narrower searches.
            async with session.get(
                f"{PIXABAY_API_BASE}/",
                params={**base_params, "page": 1},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Pixabay returned {resp.status} for query '{query}'")
                    return None
                data = await resp.json()

            total_hits = data.get("totalHits", 0) or 0
            max_page = max(1, total_hits // per_page)

            # 🌸 Only bother re-rolling if this query actually has enough
            # hits to make a second page meaningfully different — for a
            # narrow query (max_page == 1) the page=1 response above is
            # already everything there is, so a second call would just
            # waste a request for identical results.
            if max_page > 1:
                page = random.randint(1, max_page)
                if page != 1:
                    async with session.get(
                        f"{PIXABAY_API_BASE}/",
                        params={**base_params, "page": page},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                        else:
                            # 🌸 Non-fatal — just fall back to the page=1
                            # data we already fetched above instead of
                            # failing the whole request.
                            logger.warning(
                                f"Pixabay returned {resp.status} rerolling to page "
                                f"{page}/{max_page} for '{query}' — using page 1 results"
                            )
    except aiohttp.ClientError as e:
        logger.error(f"Connection error for Pixabay query '{query}': {e}")
        return None

    try:
        hits = data.get("hits", [])
        if not hits:
            logger.info(f"No Pixabay {image_type} hits found for query '{query}'")
            return None

        # 🌸 Pick whichever hit has the aspect ratio closest to a mild
        # landscape (1.3:1) rather than always taking hits[0] — Pixabay
        # illustrations/vectors are very often tall portraits (aspect
        # ratio well under 1.0), which is fine as a normal image but looks
        # broken when rendered full-height via embed.set_image() on a
        # phone screen. webformatWidth/webformatHeight are always present
        # alongside webformatURL, so this doesn't cost an extra request.
        TARGET_ASPECT = 1.3

        def _aspect_score(hit: dict) -> float:
            w = hit.get("webformatWidth") or 1
            h = hit.get("webformatHeight") or 1
            return abs((w / h) - TARGET_ASPECT)

        hit = min(hits, key=_aspect_score)
        hit_id = hit.get("id")

        # 🌸 Prefer webformatURL over largeImageURL/vectorURL for Discord
        # embedding reliability. All three are served from pixabay.com's
        # /get/ gateway (not a static CDN), which Discord's embed crawler
        # sometimes fails to resolve a content-type for in time — the
        # result is an image_url that "works" when opened manually but
        # renders as a blank/generic file-stack icon in the Discord embed
        # (width/height come back 0). webformatURL is the size Pixabay's
        # own docs describe as intended for temporary/hotlinked display,
        # so it's the most consistently embeddable of the three in
        # practice, even though it's capped at 640px. Falls back to
        # largeImageURL then vectorURL (both free-tier-safe already) if
        # webformatURL happens to be missing from a given hit.
        image_url = (
            hit.get("webformatURL")
            or hit.get("largeImageURL")
            or hit.get("vectorURL")
        )
        if not image_url:
            logger.info(f"No usable image URL in Pixabay hit for query '{query}'")
            return None
    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"Parse error for Pixabay query '{query}': {e}")
        return None

    logger.info(
        f"🎨 Media request ({kind}) | User: {message.author} ({message.author.id}) "
        f"| Query: '{query}' | image_type: {image_type} | Pixabay ID: {hit_id} | Guild: {guild_id}"
    )
    return await _build_image_embed(query, image_url, "image")
