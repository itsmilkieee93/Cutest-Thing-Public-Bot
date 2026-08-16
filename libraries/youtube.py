import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import logging
import random
import os
import re
import sys
from datetime import datetime

import yt_dlp
import aiosqlite

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")


# ── Logger (matches music_downloader style) ───────────────────────────────────
os.makedirs('log', exist_ok=True)
logger = logging.getLogger('YouTubeModule')
logger.setLevel(logging.INFO)
_log_handler = logging.FileHandler(
    'log/youtube.log', encoding='utf-8'
)
_log_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
if not logger.handlers:
    logger.addHandler(_log_handler)


# ── Loading GIFs (matches commands_ai.py style) ───────────────────────────────
LOADING_GIFS = [
    "https://c.tenor.com/knwWU-EgRmMAAAAC/tenor.gif",
    "https://c.tenor.com/J9mOaXMbKygAAAAC/tenor.gif",
    "https://c.tenor.com/plvrL3peoBIAAAAC/tenor.gif",
    "https://c.tenor.com/Yo4Vo-XCgqEAAAAC/tenor.gif",
    "https://c.tenor.com/ts-81PaXp3AAAAAC/tenor.gif",
    "https://c.tenor.com/Ly_w3cT7B04AAAAC/tenor.gif",
]


def _loading_embed(color: int) -> discord.Embed:
    embed = discord.Embed(
        title="🔎 Fetching video info…",
        description="Hang tight, pulling data via yt-dlp! ⏳",
        color=color,
    )
    embed.set_thumbnail(url=random.choice(LOADING_GIFS))
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# 💾 PER-USER INTERACTION STORE  (aiosqlite, WAL mode — matches bot_history.db)
# ══════════════════════════════════════════════════════════════════════════════
# Single DB file instead of per-user JSON files, so storage no longer depends
# on the bot process's working directory (interactions/*.json broke when the
# bot was launched from a different CWD than expected). Path is resolved
# relative to this module's own file location, not os.getcwd().
# ─────────────────────────────────────────────────────────────────────────────
DB_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "interactions")
DB_DIR  = os.path.normpath(DB_DIR)
DB_PATH = os.path.join(DB_DIR, "yt_interactions.db")
os.makedirs(DB_DIR, exist_ok=True)

_db_lock = asyncio.Lock()
_db: aiosqlite.Connection | None = None


async def _get_db() -> aiosqlite.Connection:
    """Lazily open the single shared connection and ensure sync constraints."""
    global _db
    if _db is not None:
        return _db
    async with _db_lock:
        if _db is not None:
            return _db
        conn = await aiosqlite.connect(DB_PATH)
        
        # 🩹 WAL instead of DELETE journal: readers no longer block behind a
        # writer's fsync, and writes don't need the whole-file rewrite that
        # DELETE mode does. synchronous=NORMAL is safe under WAL (durable on
        # app crash, only risks the last commit on a full OS crash — a
        # message-registry cache doesn't need EXTRA's double-fsync cost).
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS registry (
                message_id  TEXT PRIMARY KEY,
                username    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                title       TEXT,
                description TEXT,
                color       INTEGER,
                video_id    TEXT,
                channel     TEXT,
                timestamp   TEXT
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                timestamp   TEXT,
                query       TEXT,
                title       TEXT,
                channel     TEXT,
                video_id    TEXT,
                message_id  TEXT,
                guild_id    TEXT,
                guild_name  TEXT
            );
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id);"
        )
        await conn.commit()
        _db = conn
        logger.info(f"✅ SQLite DB hardened at {DB_PATH} (No more .wal/.shm caching)")
        return _db


async def _save_message_record(
    message_id: str, username: str, user_id, record: dict
) -> None:
    """Upsert button registry following the strict table constraint layout."""
    db = await _get_db()
    try:
        await db.execute(
            """
            INSERT INTO registry (message_id, username, user_id, title, description, color, video_id, channel, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                username=excluded.username,
                user_id=excluded.user_id,
                title=excluded.title,
                description=excluded.description,
                color=excluded.color,
                video_id=excluded.video_id,
                channel=excluded.channel,
                timestamp=excluded.timestamp
            """,
            (
                str(message_id), str(username), str(user_id),
                record.get("title"), record.get("description"), record.get("color"),
                record.get("video_id"), record.get("channel"), record.get("timestamp"),
            ),
        )
        await db.commit()
        # 🩹 Checkpoint right after commit so this row lands physically in
        # yt_interactions.db, not just -wal. WAL's own ~1000-page
        # auto-checkpoint (or cog_unload's clean close) doesn't fire on a
        # Termux kill / OOM / crash restart, so data that only lives in -wal
        # vanishes on reboot even though the write itself succeeded.
        # PASSIVE mode never blocks concurrent readers/writers, and this
        # button-press path isn't hot enough for the extra syscall to matter.
        try:
            await db.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except Exception as e:
            logger.warning(f"⚠️ wal_checkpoint after registry save failed: {e}")
        logger.info(f"💾 Message record {message_id} saved.")
    except Exception as e:
        logger.error(f"❌ Failed to execute upsert statement: {e}")


async def _get_message_record(message_id: str) -> dict | None:
    """Look up button data based on the exact database column order with safe casting."""
    db = await _get_db()
    try:
        # ✨ THE FIX: Menggunakan CAST agar SQLite mencocokkan ID baik dalam format teks maupun angka bulat
        async with db.execute(
            "SELECT title, description, color, video_id, channel, timestamp "
            "FROM registry WHERE CAST(message_id AS TEXT) = CAST(? AS TEXT)",
            (str(message_id),),
        ) as cursor:
            row = await cursor.fetchone()
        
        if row is None:
            logger.warning(f"⚠️ Message ID {message_id} not found in SQLite registry.")
            return None
            
        title, description, color, video_id, channel, timestamp = row
        return {
            "title":       str(title or "Unknown Title"),
            "description": str(description or ""),
            "color":       int(color) if color is not None else 0xB57EDC,
            "video_id":    str(video_id or ""),
            "channel":     str(channel or ""),
            "timestamp":   str(timestamp or ""),
        }
    except Exception as e:
        logger.error(f"❌ SQLite fetch error for message_id {message_id}: {e}")
        return None

async def _append_history(username: str, user_id, record: dict) -> None:
    """Insert one search record into the user's history inside SQLite directly."""
    db = await _get_db()
    try:
        await db.execute(
            """
            INSERT INTO history (username, user_id, timestamp, query, title, channel, video_id, message_id, guild_id, guild_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(username), str(user_id), record.get("timestamp"), record.get("query"),
                record.get("title"), record.get("channel"), record.get("video_id"),
                record.get("message_id"), record.get("guild_id"), record.get("guild_name"),
            ),
        )
        await db.commit()
        try:
            await db.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except Exception as e:
            logger.warning(f"⚠️ wal_checkpoint after history append failed: {e}")
        logger.info(f"✨ History appended successfully for user {username}.")
    except Exception as e:
        logger.error(f"❌ Failed to append search history to SQLite: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 🕐 Duration formatting helper
# ══════════════════════════════════════════════════════════════════════════════
def _fmt_duration(total_seconds) -> str:
    total = max(0, int(total_seconds or 0))
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)

    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


def _fmt_timestamp(total_seconds) -> str:
    """mm:ss or hh:mm:ss, for pointing at a spot in the video."""
    total = max(0, int(total_seconds or 0))
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ══════════════════════════════════════════════════════════════════════════════
# 🔥 Most-replayed segment helper (from yt-dlp's `heatmap` field)
# ══════════════════════════════════════════════════════════════════════════════
def _most_replayed(heatmap: list | None) -> dict | None:
    """
    yt-dlp's `heatmap` is a list of {'start_time', 'end_time', 'value'}
    dicts (value normalised 0-1). Returns the single hottest segment, or
    None if there's no heatmap data (e.g. new/low-view videos).
    """
    if not heatmap:
        return None
    try:
        peak = max(heatmap, key=lambda seg: seg.get("value", 0))
        return {
            "start": peak.get("start_time", 0),
            "end":   peak.get("end_time", 0),
            "value": peak.get("value", 0),
        }
    except (ValueError, TypeError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 📥 yt-dlp extraction (blocking — always run via asyncio.to_thread)
# ══════════════════════════════════════════════════════════════════════════════
YDL_OPTS = {
    "quiet":            True,
    "no_warnings":      True,
    "skip_download":    True,
    "extract_flat":     False,
    "noplaylist":       True,
    "logger":           logging.getLogger("yt_dlp_silent"),
    # 🩹 Skip DASH/HLS manifest + format resolution — we only need metadata
    "extractor_args": {
        "youtube": {
            "skip": ["dash", "hls", "translated_subs"],
            "player_client": ["android", "ios", "tv_embedded"],
            "player_skip": ["webpage"]
        }
    },
}

logging.getLogger("yt_dlp_silent").setLevel(logging.CRITICAL)


def _extract_info_sync(url: str) -> dict:
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        return ydl.extract_info(url, download=False)


def _extract_playlist_sync(url: str) -> dict:
    opts = dict(YDL_OPTS)
    opts["noplaylist"] = False
    opts["extract_flat"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _best_thumbnail(info: dict) -> str | None:
    """Prefer maxres, fall back down the quality ladder yt-dlp gives us."""
    thumbs = info.get("thumbnails") or []
    if thumbs:
        # yt-dlp sorts thumbnails worst→best by preference; last is best.
        maxres = [
            t for t in thumbs
            if "maxresdefault" in (t.get("url") or "")
            and (t.get("url") or "").endswith((".jpg", ".jpeg", ".png"))
        ]
        if maxres:
            return maxres[-1]["url"]
        # otherwise just take the highest-preference (last) real-image thumb
        image_thumbs = [t for t in thumbs if not (t.get("url") or "").endswith(".webp")]
        if image_thumbs:
            return image_thumbs[-1]["url"]
        return thumbs[-1]["url"]
    return info.get("thumbnail")


# ══════════════════════════════════════════════════════════════════════════════
# 🎬 YouTube Button View — persistent across restarts
# ══════════════════════════════════════════════════════════════════════════════
class YouTubeView(discord.ui.View):
    def __init__(self):
        # ✨ KOSONGKAN PARAMETER AGAR LAYOUT CUSTOM_ID SELALU STATIS PASCA REBOOT
        super().__init__(timeout=None)

    @discord.ui.button(
        label="View Description",
        style=discord.ButtonStyle.secondary,
        emoji="📄",
        custom_id="yt:view_description"
    )
    async def view_description(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        logger.info(
            f"🖱️ view_description clicked | msg="
            f"{interaction.message.id if interaction.message else '?'} "
            f"user={interaction.user.id}"
        )

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            logger.warning(
                f"⚠️ view_description defer failed for msg "
                f"{interaction.message.id if interaction.message else '?'}: {e}"
            )
            return

        async def _reply(embed: discord.Embed):
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(
                    f"❌ view_description followup.send failed for msg "
                    f"{interaction.message.id if interaction.message else '?'}: {e}",
                    exc_info=True,
                )

        try:
            if interaction.message is None:
                return await _reply(discord.Embed(
                    title="❌ Message Unavailable",
                    description="Couldn't retrieve the message. Please run `/youtube` again! 😔",
                    color=0xFF6B6B
                ))

            # Database lookup using custom cast function
            record = await _get_message_record(str(interaction.message.id))

            if not record:
                return await _reply(discord.Embed(
                    title="❌ Data Not Found",
                    description=(
                        "This video's data couldn't be found. 😔\n"
                        "Please run `/youtube` again to get fresh results!"
                    ),
                    color=0xFF6B6B
                ))

            title = record.get("title", "Unknown")
            desc  = record.get("description", "").strip()
            color = record.get("color", 0xB57EDC)

            if not isinstance(color, int):
                color = 0xB57EDC

            if not desc:
                return await _reply(discord.Embed(
                    title="📄 No Description",
                    description="This video has no description. 😊",
                    color=color
                ))

            if len(desc) > 4000:
                desc = desc[:4000] + "\n\n*...Description truncated ✂️*"


            embed = discord.Embed(
                title=f"📄 {title}"[:256],
                description=desc,
                color=color
            )
            embed.set_footer(
                text="YouTube ✨️",
                icon_url=(
                    "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e7/"
                    "YouTube_social_white_squircle_%282024%29.svg/"
                    "1280px-YouTube_social_white_squircle_%282024%29.svg.png"
                )
            )
            await _reply(embed)

        except Exception as e:
            logger.error(f"❌ view_description error: {e}", exc_info=True)
            await _reply(discord.Embed(
                title="❌ Something went wrong",
                description=f"```{str(e)[:200]}```",
                color=0xFF6B6B
            ))


# ══════════════════════════════════════════════════════════════════════════════
# 🎬 YouTube Universal Module Cog
# ══════════════════════════════════════════════════════════════════════════════
PASTEL_COLORS = [
    0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF, 0xB5EAD7,
    0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xFFB7CE, 0xA8D8EA,
    0xFDFD96, 0xE0BBE4, 0x957DAD, 0xD4F0F0, 0xFFE5B4,
    0xE2F0CB, 0xFFCCF9, 0xC5E1A5, 0xF4978E, 0xB8E1FF,
    0xFFE4E1, 0xE6E6FA, 0xFADADD, 0xAFEEEE, 0xFFF9B1,
    0xDBD7FB, 0xB2F2BB, 0xFFD8B1, 0xD0F0C0, 0xC1E1C1,
    0xF0EAD6, 0xFFD1B3, 0xE9FFDB, 0xCFE2F3, 0xDEB887,
    0xFFC3A0, 0xD1D1D1, 0xFBC4AB, 0xFFEBCD, 0xC3B1E1,
]




class YouTubeUniversalModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("🧩 YouTubeUniversalModule cog initialized.")

    async def cog_load(self):
        """Memaksa registrasi View Persisten global saat siklus memori Cog aktif."""
        self.bot.add_view(YouTubeView())
        logger.info("✅ Persistent YouTubeView successfully mapped inside cog_load!")

    async def cog_unload(self):
        global _db
        if _db is not None:
            await _db.close()
            _db = None
            logger.info("🔌 aiosqlite connection closed on cog unload.")

    async def yt_autocomplete(self, interaction: discord.Interaction, current: str):
        if not current:
            return []
        url = "https://suggestqueries.google.com/complete/search"
        params = {'client': 'firefox', 'ds': 'yt', 'q': current}
        try:
            async with self.bot.session.get(url, params=params) as resp:
                data = await resp.json(content_type=None)
                return [
                    app_commands.Choice(name=s, value=s)
                    for s in data[1][:10]
                ]
        except Exception as e:
            logger.warning(f"⚠️ Autocomplete error: {e}")
            return []

    @app_commands.command(name="youtube", description="Get video/playlist metadata via yt-dlp! ⚡")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Paste a link or search for something...")
    @app_commands.autocomplete(query=yt_autocomplete)
    async def yt_universal(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        req_user = interaction.user
        req_name = str(req_user)
        req_id   = str(req_user.id)

        ui_color = random.choice(PASTEL_COLORS)

        # ── Show a loading embed immediately (gif thumbnail) ────────────────
        # 🩹 THE FIX: was `interaction.followup.send(..., wait=True)`, which
        # posts a SEPARATE new message and leaves the deferred "Cutest Thing
        # is thinking..." placeholder unresolved forever. ~15 min later that
        # orphaned placeholder's token expires and Discord's client renders
        # it as "Cutest Thing didn't respond in time" — a ghost message sent
        # on EVERY /youtube call, independent of how long extraction took.
        # `edit_original_response()` updates that placeholder in place
        # instead of creating a second message, so there's nothing left to
        # ever expire/orphan.
        await interaction.edit_original_response(embed=_loading_embed(ui_color))
        loading_msg = await interaction.original_response()

        video_id    = None
        playlist_id = None
        target_url  = None

        q = query.strip()

        _pl = re.search(r'[?&]list=([A-Za-z0-9_-]+)', q)
        _v  = re.search(
            r'(?:'
            r'[?&]v=([A-Za-z0-9_-]{11})'
            r'|youtu\.be/([A-Za-z0-9_-]{11})'
            r'|/shorts/([A-Za-z0-9_-]{11})'
            r'|/live/([A-Za-z0-9_-]{11})'
            r'|/embed/([A-Za-z0-9_-]{11})'
            r'|/v/([A-Za-z0-9_-]{11})'
            r')',
            q,
        )

        if _pl and not _v:
            playlist_id = _pl.group(1)
            target_url  = f"https://www.youtube.com/playlist?list={playlist_id}"
        elif _v:
            video_id   = next(g for g in _v.groups() if g)
            target_url = f"https://www.youtube.com/watch?v={video_id}"
            if _pl:
                playlist_id = _pl.group(1)
        elif re.fullmatch(r'[A-Za-z0-9_-]{11}', q):
            video_id   = q
            target_url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            # Not a direct link/ID — treat as a search term, resolve via yt-dlp's
            # own search extractor rather than a separate API call.
            target_url = f"ytsearch1:{q}"

        try:
            embed = discord.Embed(color=ui_color)
            info  = None

            # ── CASE 1: PLAYLIST ─────────────────────────────────────────────
            if playlist_id:
                view = discord.ui.View()

                info = await asyncio.to_thread(_extract_playlist_sync, target_url)
                if not info:
                    return await loading_msg.edit(embed=discord.Embed(
                        title="❌ Playlist not found",
                        description="Playlist not found or is unavailable.",
                        color=0xFF6B6B
                    ))

                entries    = info.get("entries") or []
                pl_title   = info.get("title", "Unknown Playlist")
                pl_channel = info.get("uploader") or info.get("channel") or "Unknown"

                embed.title = f"📂 Playlist: {pl_title}"
                embed.add_field(name="👤 Creator",      value=pl_channel,                      inline=True)
                embed.add_field(name="📦 Total Videos", value=str(len(entries)),               inline=True)
                embed.add_field(name="🔗 Link",          value=f"[Open Playlist]({target_url}) ✨", inline=True)

                thumb = None
                if entries:
                    first_id = entries[0].get("id")
                    if first_id:
                        thumb = f"https://i.ytimg.com/vi/{first_id}/maxresdefault.jpg"
                if thumb:
                    embed.set_image(url=thumb)

                view.add_item(discord.ui.Button(
                    label="View Playlist",
                    url=target_url,
                    style=discord.ButtonStyle.link,
                    emoji="📂"
                ))

            # ── CASE 2: VIDEO / SHORT / SEARCH ──────────────────────────────
            else:
                info = await asyncio.to_thread(_extract_info_sync, target_url)
                if not info:
                    return await loading_msg.edit(embed=discord.Embed(
                        title="❌ Video not found",
                        description="No videos found for that search/link.",
                        color=0xFF6B6B
                    ))

                # ytsearch1: returns a playlist-shaped wrapper with 1 entry
                if info.get("_type") == "playlist":
                    entries = info.get("entries") or []
                    if not entries:
                        return await loading_msg.edit(embed=discord.Embed(
                            title="❌ No results",
                            description="No videos found for that search query.",
                            color=0xFF6B6B
                        ))
                    info = entries[0]

                video_id   = info.get("id")
                target_url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
                view = YouTubeView()
                view.add_item(discord.ui.Button(
                    label="View Video",
                    url=target_url,
                    style=discord.ButtonStyle.link,
                    emoji="📲"
                ))

                title    = info.get("title") or info.get("fulltitle") or "Unknown Title"
                channel  = info.get("uploader") or info.get("channel") or "Unknown"
                is_short = "/shorts/" in (info.get("webpage_url") or q)

                view_count    = info.get("view_count")
                like_count    = info.get("like_count")
                comment_count = info.get("comment_count")
                duration_s    = info.get("duration")

                upload_date = info.get("upload_date")  # YYYYMMDD
                if upload_date:
                    try:
                        date_obj = datetime.strptime(upload_date, "%Y%m%d")
                        pub_date = date_obj.strftime("%A, %d %B %Y")
                    except ValueError:
                        pub_date = "Unknown"
                else:
                    pub_date = "Unknown"

                embed.title = f"{'📱 Short' if is_short else '🎥 Video'}: {title}"

                views_val    = f"{int(view_count):,}"    if view_count    is not None else "Hidden"
                likes_val    = f"{int(like_count):,}"    if like_count    is not None else "Hidden"
                comments_val = f"{int(comment_count):,}" if comment_count is not None else "Disabled"

                embed.add_field(name="👤 Author",         value=channel,                        inline=True)
                embed.add_field(name="📅 Date Published", value=pub_date,                       inline=True)
                embed.add_field(name="⏱️ Duration",       value=_fmt_duration(duration_s),      inline=True)
                embed.add_field(name="👀 Views",          value=views_val,                       inline=True)
                embed.add_field(name="👍 Likes",          value=likes_val,                       inline=True)
                embed.add_field(name="💬 Comments",       value=comments_val,                    inline=True)

                # ── Most replayed part (from heatmap, if YouTube has one) ──
                peak = _most_replayed(info.get("heatmap"))
                if peak:
                    ts_link = f"{target_url}&t={int(peak['start'])}s" if "watch?v=" in target_url else target_url
                    embed.add_field(
                        name="🔥 Most Replayed",
                        value=f"[{_fmt_timestamp(peak['start'])}–{_fmt_timestamp(peak['end'])}]({ts_link})",
                        inline=True
                    )

                embed.add_field(name="🆔 Video ID", value=video_id, inline=True)

                thumb = _best_thumbnail(info)
                if thumb:
                    embed.set_image(url=thumb)

            embed.set_footer(
                text="YouTube • via yt-dlp ✨️",
                icon_url="https://upload.wikimedia.org/wikipedia/commons/thumb/e/e7/YouTube_social_white_squircle_%282024%29.svg/1280px-YouTube_social_white_squircle_%282024%29.svg.png"
            )


            # ── Swap the loading embed for the real result ─────────────────
            await loading_msg.edit(embed=embed, view=view)

            # ── Persist per-user record (same pattern as music_downloader) ─
            if video_id and info:
                title       = info.get("title") or "Unknown Title"
                description = info.get("description", "") or ""
                channel     = info.get("uploader") or info.get("channel") or ""

                registry_record = {
                    "title":       title,
                    "description": description,
                    "color":       ui_color,
                    "video_id":    video_id,
                    "channel":     channel,
                    "timestamp":   datetime.now().isoformat(),
                }
                # 1. Button registry (for View Description after restart)
                await _save_message_record(
                    str(loading_msg.id), req_name, req_id, registry_record
                )
                # 2. Search history
                await _append_history(
                    req_name, req_id,
                    {
                        "timestamp":  datetime.now().isoformat(),
                        "query":      query,
                        "title":      title,
                        "channel":    channel,
                        "video_id":   video_id,
                        "message_id": str(loading_msg.id),
                        "guild_id":   str(interaction.guild_id or "DM"),
                        "guild_name": interaction.guild.name if interaction.guild else "DM",
                    }
                )

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"❌ yt-dlp DownloadError for {req_user}: {e}")
            await loading_msg.edit(embed=discord.Embed(
                title="❌ Couldn't fetch that",
                description=f"```{str(e)[:200]}```",
                color=0xFF6B6B
            ))
        except Exception as e:
            logger.error(f"❌ yt_universal error for {req_user}: {e}", exc_info=True)
            await loading_msg.edit(embed=discord.Embed(
                title="❌ Something went wrong",
                description=f"```{str(e)[:200]}```",
                color=0xFF6B6B
            ))


async def setup(bot):
    await bot.add_cog(YouTubeUniversalModule(bot))
    print("✅ YouTube Module loaded successfully! (Fixed Cog-Load Lifecycle)")
