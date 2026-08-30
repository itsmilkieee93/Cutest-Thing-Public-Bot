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
        
  (2026 update: video results went BACK to a plain str — a bare .mp4
  CDN link, no embed wrapper, no caption. Discord's own crawler renders
  a native inline video player for a bare link; wrapping it in a
  discord.Embed just produces a broken/blank image box since embeds
  don't support a true playable video field. See _build_video_reply().)
  - 45% of the time (CAPTION_CHANCE), a short cute one-liner from a second
    quick Groq call becomes the embed's description. Photo/vector/
    cartoon only — video has no caption at all now.
  - Photo (Pexels) / vector / cartoon (Pixabay) results are returned as a
    discord.Embed — set_image() (no raw link text visible, unlike a
    bare-URL reply where Discord shows the link text above its own
    auto-generated embed). Both providers now get Discord-style linked
    attribution: embed.title links to the original post (Pexels photo
    page / Pixabay pageURL) and embed.set_author() links the
    photographer's/uploader's name to their profile, with an avatar icon
    when the provider has one (Pixabay only — Pexels has no avatar
    field). Pexels photos additionally use avg_color as the embed's
    accent color instead of the flat kawaii pink.
  - Video results are a bare str (the direct .mp4 link) — see
    _build_video_reply().
  - Pexels: photo["src"]["large2x"] for the image, photo["url"] for the
    post link, photo["photographer"]/["photographer_url"] for the
    author byline, photo["avg_color"] for the embed accent color, and a
    direct .mp4 video_files link for videos. Pixabay: hit["webformatURL"]
    for the image (falling back to hit["largeImageURL"]/["vectorURL"]),
    hit["pageURL"] for the post link, hit["user"]/["user_id"] for the
    author byline (profile URL is built manually — Pixabay doesn't
    return one), hit["userImageURL"] for the author avatar.
  - Returns None if classification says "not a media request", the
    relevant API key is missing, the request fails, or nothing is found —
    caller falls through to the next interceptor / Groq's full reply as
    normal. Otherwise returns a discord.Embed (photo/vector/cartoon) or a
    bare str (video) — bot_service.py's reply site must check
    isinstance(intercepted, discord.Embed) to decide between
    message.reply(embed=...) and message.reply(content=...).

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
    # 🌸 Also stream to stdout (dynamically, so it picks up log_webhook's
    # sys.stdout Tee even if start_log_webhook() runs after this import)
    class _LiveStdoutStream:
        def write(self, msg):
            sys.stdout.write(msg)
        def flush(self):
            sys.stdout.flush()
    _stdout_handler = logging.StreamHandler(_LiveStdoutStream())
    _stdout_handler.setFormatter(logging.Formatter('[GroqPexels] [%(levelname)s] %(message)s'))
    logger.addHandler(_stdout_handler)

PEXELS_API_BASE = "https://api.pexels.com"
PIXABAY_API_BASE = "https://pixabay.com/api"

# 🌸 Kinds the classifier is allowed to return. "vector" and "cartoon" both
# route to Pixabay (different image_type), "photo"/"video" route to Pexels.
VALID_KINDS = {"photo", "video", "vector", "cartoon"}

# 🌸 Local fallback net for the anime/manga -> "cartoon" rule (see
# CLASSIFY_SYSTEM_PROMPT). Pexels is a real-photography stock library —
# an "anime image" search against it surfaces things like cosplay
# photoshoots or convention photos, which LOOK related by tag/keyword
# but are the opposite of what "anime image" means (drawn/illustrated
# art). This catches it locally even on the rare classifier miss.
ANIME_STYLE_PATTERN = re.compile(
    r"\b(anime|manga|chibi|waifu|husbando)\b", re.IGNORECASE
)


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
CLASSIFY_MODEL = "openai/gpt-oss-20b"

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
    "style/ANIME/manga/chibi/waifu requests; 'video'/'clip' wording -> "
    "'video'; otherwise 'photo' is the default for real-photo requests.\n"
    "- CRITICAL: anime, manga, chibi, and waifu are ALWAYS 'cartoon', "
    "NEVER 'photo' — even when the message just says 'anime image' or "
    "'anime picture' with no other style words. These are drawn/"
    "illustrated art styles, not real photography, and must route to "
    "the illustration search, not the stock-photo search. A real "
    "photo of a person cosplaying an anime character is NOT what the "
    "user means by 'anime image' — they mean actual anime-style "
    "artwork.\n"
    "- query is the bare subject only — no style words, no filler like "
    "'please'/'pls', no leading articles ('a'/'an'/'the'), lowercase.\n"
    "- If the message is NOT a media request (e.g. it's a question, "
    "small talk, or asks the bot to draw/generate/edit an image itself "
    "rather than fetch a stock one), set is_media_request to false and "
    "kind/query to null.\n"
    "- Never include commentary, markdown, or text outside the JSON "
    "object. \n"
    "CRITICAL RULE: If the user is asking for a SONG, MUSIC, TRACK, or AUDIO "
    "(e.g., 'give me a song by...'), you MUST set is_media_request to false and kind/query to null. "
    "Songs are NOT visual media requests."
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
            # 🌸 openai/gpt-oss-20b is a REASONING model — it spends tokens
            # on hidden chain-of-thought before emitting content, so
            # reasoning_effort="low" + generous max_tokens is required or
            # it can burn the whole budget before ever reaching valid JSON.
            # This is exactly what "Failed to generate JSON... max
            # completion tokens reached before generating a valid document"
            # meant — max_tokens=80 with default (medium) reasoning_effort
            # left no room for the actual JSON object. Same root cause as
            # the Aug 2026 music-classifier outage in groq_music_suggestion.py.
            reasoning_effort="low",
            max_tokens=1000,
            response_format={"type": "json_object"},
        )

    try:
        response = await asyncio.to_thread(_call)
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            # 🌸 Same empty-content symptom the music classifier hit —
            # flag it distinctly from a genuine JSON parse error so a
            # reasoning-token regression like this is obvious in logs
            # instead of looking like a one-off malformed response.
            logger.warning("Media request classification returned EMPTY content — check reasoning_effort/max_tokens")
            return None
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

    # 🌸 Belt-and-suspenders on top of the prompt's own anime/manga rule
    # above — a probabilistic classifier can still miss it occasionally
    # (see the "anime image" -> real cosplay photo mixup), and getting
    # this specific case wrong is the single most visible failure mode
    # (a real photo when the user very clearly wanted drawn art). Cheap
    # local regex check on the ORIGINAL message content, not just the
    # extracted query, so it still catches "anime" even if the model
    # trimmed it out of the query string itself.
    if kind == "photo" and ANIME_STYLE_PATTERN.search(content):
        kind = "cartoon"

    return {"is_media_request": True, "kind": kind, "query": query}


def _resolve_pixabay_image_type(kind: str) -> str:
    return "vector" if kind == "vector" else "illustration"


# ── Caption generation (unchanged behavior, kept independent of the full
#    GroqService personality/memory pipeline — cheap throwaway flavor) ────

CAPTION_CHANCE = 0.45
CAPTION_MODEL = "openai/gpt-oss-20b"

CAPTION_SYSTEM_PROMPT = (
    "You are Cutest Thing, a kawaii-themed Discord bot with a pink/purple "
    "sparkly aesthetic. You just found a photo or video for someone. Write "
    "ONE short, cute, enthusiastic caption/reaction to go above the media "
    "link — under 12 words, casual, can use 1-2 emoji (🌸💖✨🎀). No "
    "quotation marks, no markdown, just the line itself. Don't describe "
    "the image in detail since you haven't actually seen it yet — just "
    "react with excitement about finding it."
)


async def _maybe_caption_text(query: str, kind: str, force: bool = False) -> str | None:
    """🌸 With CAPTION_CHANCE probability (or always, if force=True), asks
    Groq for a short cute caption. Returns the caption string, or None if
    the dice roll didn't hit / Groq call failed / result was empty —
    callers decide what to do with a None (image path puts it in the
    embed description if present; video path prepends it above the bare
    link if present).

    kind is used only for the log line / prompt phrasing.
    """
    if not force and random.random() >= CAPTION_CHANCE:
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
            # 🌸 Same reasoning-model headroom fix as _classify_media_request
            # above — 30 tokens with default reasoning_effort risked an
            # empty caption (silently skipped, which is low-stakes here,
            # but no reason to leave the same bug lurking twice).
            reasoning_effort="low",
            max_tokens=120,
        )

    try:
        response = await asyncio.to_thread(_call)
        caption = (response.choices[0].message.content or "").strip()
        caption = caption.strip('"').strip("'").strip()
        return caption or None
    except Exception as e:
        logger.warning(f"Caption generation failed ({kind}, query='{query}'): {e}")
        return None


async def _build_image_embed(
    query: str,
    image_url: str,
    kind: str,
    post_url: str | None = None,
    post_title: str | None = None,
    author_name: str | None = None,
    author_url: str | None = None,
    author_avatar_url: str | None = None,
    color: int = 0xFFB6D9,
    provider: str = "Pexels",
) -> discord.Embed:
    """🌸 Builds a discord.Embed with the image set via set_image() — this
    is what actually hides the raw link text; a bare-URL reply always
    shows the link above Discord's own auto-embed, but embed.set_image()
    renders the image with nothing else visible. The optional cute
    caption (per CAPTION_CHANCE) becomes the embed description instead
    of a prepended text line.

    Attribution — rewritten to satisfy Pexels' literal required phrasing
    ("Photo by [Name] on Pexels", linked to the photo page) as ONE
    unified, immediately-visible credit line rather than splitting the
    name and the "on Pexels"/"on Pixabay" part across embed.title and
    embed.set_author() the way this used to work. A reviewer (or a user)
    glancing at the embed without clicking anything now sees the full
    phrase at once, in embed.set_author():
      - name  -> "Photo by {author_name} on {provider}" (or "Image by
        ..." for Pixabay/vector/cartoon, since those aren't literally
        "photos").
      - url   -> the photo/post page itself (post_url, e.g. the Pexels
        /photo/... page), NOT the photographer's profile — Pexels'
        guideline is specifically "with a link to the photo page on
        Pexels", not to the profile. This is also more useful to a user
        wanting to see the original.
      - icon_url -> author_avatar_url when the provider has one
        (Pixabay's userImageURL; Pexels has no avatar field).
    embed.title is no longer used for attribution at all — it's freed up
    for the caption/description to do its normal job, so the credit line
    can't be mistaken for a generic title.
    """
    caption = await _maybe_caption_text(query, kind)

    embed = discord.Embed(
        description=caption if caption else None,
        color=color,
    )

    if author_name and post_url:
        noun = "Photo" if provider == "Pexels" else "Image"
        embed.set_author(
            name=f"{noun} by {author_name} on {provider}",
            url=post_url,
            icon_url=author_avatar_url or None,
        )
    elif author_name:
        # 🌸 Fallback if a post_url is somehow missing — still show the
        # required phrase, just without it being a clickable link.
        noun = "Photo" if provider == "Pexels" else "Image"
        embed.set_author(name=f"{noun} by {author_name} on {provider}")

    embed.set_image(url=image_url)
    return embed


async def _build_video_reply(query: str, video_url: str) -> str:
    """🌸 Video results are sent as a BARE .mp4 CDN link — no
    discord.Embed wrapper, no AI caption, no CAPTION_CHANCE roll. Discord
    embeds don't support a playable video field the way set_image() works
    for stills (set_image() on a .mp4 just renders blank/broken), so the
    only way to get Discord's native inline video player is a plain link
    with nothing else around it. Returning a bare str (not a discord.Embed)
    is what triggers that — the caller sends it as message content.
    """
    return video_url


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


# ── Pexels photographer avatar lookup ──────────────────────────────────────
#
# 🌸 The Pexels API's Photo object does NOT include an avatar field (only
# photographer / photographer_id / photographer_url) — confirmed against
# their documented schema. The avatar image DOES exist on-CDN at
# images.pexels.com/users/avatars/{photographer_id}/{slug}-{n}.jpeg, but
# the {slug}-{n} part isn't derivable from anything the API gives us, so
# it can't be constructed — it has to be read off the photographer's
# public profile page (which photographer_url already points to).
#
# Cached per photographer_id so this is a one-time cost per photographer,
# not a per-photo-request cost — most queries will re-hit a handful of
# popular photographers repeatedly. Failures (missing id, page fetch
# error, no <img> match) are cached as None too, so a photographer with
# no parseable avatar doesn't get re-fetched on every future request for
# them either.
_avatar_cache: dict[int, str | None] = {}
_AVATAR_IMG_RE = re.compile(
    r'https://images\.pexels\.com/users/avatars/\d+/[^"\'\s]+\.(?:jpe?g|png)'
)


async def _get_pexels_avatar_url(photographer_id: int | None, profile_url: str | None) -> str | None:
    """🌸 Best-effort fetch of a photographer's avatar CDN URL by scraping
    their public Pexels profile page for the first images.pexels.com/
    users/avatars/... URL. Returns None on any failure — this is purely
    cosmetic (an icon next to the credit line), never something that
    should block or fail the actual embed."""
    if not photographer_id or not profile_url:
        return None

    if photographer_id in _avatar_cache:
        return _avatar_cache[photographer_id]

    avatar_url = None
    try:
        async with aiohttp.ClientSession() as session:
            logger.info(f"→ GET pexels.com (profile scrape) | photographer_id={photographer_id}")
            async with session.get(
                profile_url,
                timeout=aiohttp.ClientTimeout(total=1),
                headers={"User-Agent": "Mozilla/5.0 (compatible; CutestThingBot/1.0)"},
            ) as resp:
                logger.info(f"← GET pexels.com (profile scrape) | status={resp.status} | photographer_id={photographer_id}")
                if resp.status == 200:
                    html = await resp.text()
                    match = _AVATAR_IMG_RE.search(html)
                    if match:
                        avatar_url = match.group(0)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.info(f"Avatar lookup failed for photographer_id={photographer_id}: {e}")
    except Exception as e:
        # 🌸 Deliberately broad — this is a nice-to-have scrape against an
        # undocumented, unversioned HTML page. Any parse weirdness here
        # should degrade to "no avatar", never break the photo reply.
        logger.info(f"Unexpected avatar lookup error for photographer_id={photographer_id}: {e}")

    _avatar_cache[photographer_id] = avatar_url
    return avatar_url


# ── Single entry point: classify once, dispatch to the right API ──────────

async def handle_media_request(message, guild_id: int, shared) -> discord.Embed | str | None:
    """
    🌸 AI-CLASSIFIED MEDIA HANDLER — the single interceptor entry point.
    Replaces the old handle_pexels_photo_request / handle_pexels_video_
    request / handle_pixabay_style_request trio with one classifier call
    that decides intent + kind + query, then dispatches to Pexels (photo/
    video) or Pixabay (vector/cartoon).

    Returns:
      - discord.Embed for photo (Pexels) / vector / cartoon (Pixabay).
      - a bare str (the raw .mp4 CDN link, no caption) for video, so
        Discord's own embed crawler renders a native inline video player
        instead of a broken image box inside a discord.Embed.
      - None if the message isn't a media request / the relevant API key
        is missing / the request fails / nothing is found — caller falls
        through to the next interceptor or Groq's full reply normally.

    bot_service.py's reply site must check isinstance(result,
    discord.Embed) to know whether to call message.reply(embed=...) or
    message.reply(content=...) (str case, video).
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
            logger.info(f"→ GET api.pexels.com/v1/search | query={query!r}")
            async with session.get(
                f"{PEXELS_API_BASE}/v1/search",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                logger.info(f"← GET api.pexels.com/v1/search | status={resp.status} | query={query!r}")
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
        image_url = photo["src"]["original"]
        photo_id = photo.get("id")
        post_url = photo.get("url")
        photographer = photo.get("photographer")
        photographer_url = photo.get("photographer_url")
        photographer_id = photo.get("photographer_id")

        # 🌸 avg_color comes back as "#RRGGBB" — discord.Embed(color=...)
        # wants an int, so strip the # and base-16 parse it. Falls back
        # to the flat kawaii pink if avg_color is missing/malformed for
        # some reason (shouldn't normally happen on a Pexels photo hit).
        avg_color_hex = photo.get("avg_color")
        try:
            embed_color = int(avg_color_hex.lstrip("#"), 16) if avg_color_hex else 0xFFB6D9
        except (ValueError, AttributeError):
            embed_color = 0xFFB6D9
    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"Parse error for Pexels photo query '{query}': {e}")
        return None

    logger.info(
        f"📷 Media request (photo) | User: {message.author} ({message.author.id}) "
        f"| Query: '{query}' | Photo ID: {photo_id} | Guild: {guild_id}"
    )

    # 🌸 Best-effort avatar lookup — see _get_pexels_avatar_url(). Never
    # raises; None just means no icon on the credit line, same as before.
    avatar_url = await _get_pexels_avatar_url(photographer_id, photographer_url)

    return await _build_image_embed(
        query, image_url, "photo",
        post_url=post_url,
        author_name=photographer,
        author_avatar_url=avatar_url,
        color=embed_color,
        provider="Pexels",
    )


async def _fetch_pexels_video(message, guild_id: int, query: str) -> str | None:
    """🌸 Hits Pexels /videos/search and returns the bare .mp4 CDN link
    as a plain string (no embed, no caption) so Discord's native inline
    video player picks it up, or None on any failure/empty result."""
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
            logger.info(f"→ GET api.pexels.com/videos/search | query={query!r}")
            async with session.get(
                f"{PEXELS_API_BASE}/videos/search",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                logger.info(f"← GET api.pexels.com/videos/search | status={resp.status} | query={query!r}")
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
    return await _build_video_reply(query, video_url)


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
        # 🌸 Always "popular" — NOT randomized between popular/latest
        # anymore. "latest" pulls from Pixabay's newest uploads, which is
        # a much less curated pool (anyone can upload/tag loosely), and
        # was the main source of off-topic/low-quality results slipping
        # through (e.g. a random 3D render surfacing for an anime query).
        # Variety now comes purely from the random page + aspect-ratio
        # pick below, without sacrificing result relevance.
        "order":       "popular",
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
            logger.info(f"→ GET pixabay.com/api | query={query!r} | page=1")
            async with session.get(
                f"{PIXABAY_API_BASE}/",
                params={**base_params, "page": 1},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                logger.info(f"← GET pixabay.com/api | status={resp.status} | query={query!r} | page=1")
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
                    logger.info(f"→ GET pixabay.com/api | query={query!r} | page={page}")
                    async with session.get(
                        f"{PIXABAY_API_BASE}/",
                        params={**base_params, "page": page},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        logger.info(f"← GET pixabay.com/api | status={resp.status} | query={query!r} | page={page}")
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

        # 🌸 Quality floor: even under order="popular", a deep random page
        # can land on hits with near-zero engagement — often mistagged or
        # off-topic uploads (a big source of the "sometimes weird image"
        # problem). Prefer hits with at least a modest number of likes
        # when the batch has any; if literally everything on this page is
        # low-engagement (a genuinely niche query), fall back to using
        # all of them rather than returning nothing.
        MIN_LIKES = 5
        well_liked = [h for h in hits if (h.get("likes") or 0) >= MIN_LIKES]
        candidates = well_liked if well_liked else hits

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

        hit = min(candidates, key=_aspect_score)
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

        # 🌸 Attribution — same discord-style linked title/author pattern
        # as the Pexels photo path. pageURL is the actual Pixabay post;
        # Pixabay doesn't hand back a ready-made profile URL like Pexels'
        # photographer_url does, so it's built from the documented
        # /users/{user}-{user_id}/ pattern using "user" + "user_id" off
        # the same hit. userImageURL becomes the author icon.
        post_url = hit.get("pageURL")
        credit_user = hit.get("user")
        credit_user_id = hit.get("user_id")
        credit_avatar_url = hit.get("userImageURL")
        author_url = (
            f"https://pixabay.com/users/{credit_user}-{credit_user_id}/"
            if credit_user and credit_user_id else None
        )
    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"Parse error for Pixabay query '{query}': {e}")
        return None

    logger.info(
        f"🎨 Media request ({kind}) | User: {message.author} ({message.author.id}) "
        f"| Query: '{query}' | image_type: {image_type} | Pixabay ID: {hit_id} | Guild: {guild_id}"
    )
    return await _build_image_embed(
        query, image_url, "image",
        post_url=post_url,
        author_name=credit_user,
        author_avatar_url=credit_avatar_url,
        provider="Pixabay",
    )
