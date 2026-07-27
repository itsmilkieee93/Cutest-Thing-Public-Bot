# encoding: utf-8
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import os
import re
import asyncio
import aiohttp
import random
import logging
import time
import json
import traceback
from datetime import datetime
from ytmusicapi import YTMusic
from typing import List


# ─── Error code helper ───────────────────────────────────────────────────────
def _err_code(e: Exception) -> str:
    """Extract an HTTP status code from the error string, or return a mapped code."""
    match = re.search(r'\b([45]\d{2})\b', str(e))
    if match:
        return f"HTTP {match.group(1)}"
    code_map = {
        "ValueError":        "E001",
        "FileNotFoundError": "E002",
        "DownloadError":     "E003",
        "PermissionError":   "E004",
        "TimeoutError":      "E005",
        "ConnectionError":   "E006",
        "OSError":           "E007",
    }
    name = type(e).__name__
    for key, code in code_map.items():
        if key in name:
            return code
    return f"E{abs(hash(name)) % 900 + 100:03d}"


# ─── Logger ───────────────────────────────────────────────────────────────────
os.makedirs('log',  exist_ok=True)
os.makedirs('temp', exist_ok=True)
logger = logging.getLogger('MusicDownloader')
logger.setLevel(logging.INFO)
handler = logging.FileHandler(
    '/sdcard/script/bot/log/music_downloader.log', encoding='utf-8'
)
handler.setFormatter(logging.Formatter(
    '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
if not logger.handlers:
    logger.addHandler(handler)

# ─── YTMusic (safe init) ───────────────────────────────────────────────────────
try:
    ytm = YTMusic()
except Exception as _ytm_err:
    ytm = None
    logger.warning(f"YTMusic init failed: {_ytm_err}")

# ─── Hardware acceleration ────────────────────────────────────────────────────
HW_SUPPORTED_FORMATS = {'m4a'}
FAST_ENCODE_ARGS = ['-threads', '0', '-resampler', 'soxr', '-compression_level', '0']
HW_ENCODE_ARGS   = ['-hwaccel', 'mediacodec', '-hwaccel_output_format', 'nv12']

# ─── Greeting & Ready Emojis ──────────────────────────────────────────────────
GREETING_EMOJI = ["🎉", "🌸", "✨", "💖", "🎶", "🥰", "💫", "🎀", "🌟", "🍀", "💞", "🎊", "👋", "😊"]
READY_EMOJI    = ["🎵", "🎧", "🎤", "🎼", "🎹", "🥳", "💿", "🌈", "🔥", "💎", "🫶", "🙌"]

# ══════════════════════════════════════════════════════════════════════════════
# 💾 PER-USER INTERACTION STORE
# ══════════════════════════════════════════════════════════════════════════════
# File layout:
#   interactions/
#     StayHalalBro_123456_interaction.json   ← each user's history + button registry
#     message_index.json                     ← message_id → {username, user_id}
#                                               (needed for button lookups after restart)
# ─────────────────────────────────────────────────────────────────────────────
INTERACTIONS_DIR = "interactions"
os.makedirs(INTERACTIONS_DIR, exist_ok=True)


def _clean_name(username: str) -> str:
    """Sanitise a Discord username for safe use in a filename."""
    return re.sub(r'[^\w]', '_', str(username))[:24].strip('_') or 'user'


def _user_file(username: str, user_id) -> str:
    return os.path.join(INTERACTIONS_DIR, f"{_clean_name(username)}_{user_id}_interaction.json")


def _index_file() -> str:
    return os.path.join(INTERACTIONS_DIR, "message_index.json")


def _atomic_write(path: str, data: dict | list) -> None:
    """Write JSON atomically so a crash mid-write never corrupts data."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"❌ Write failed [{path}]: {e}")
        try: os.remove(tmp)
        except OSError: pass


def _load_user_data(username: str, user_id) -> dict:
    path = _user_file(username, user_id)
    if not os.path.exists(path):
        return {
            "username": str(username),
            "user_id":  str(user_id),
            "history":  [],
            "registry": {}
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "username": str(username),
            "user_id":  str(user_id),
            "history":  [],
            "registry": {}
        }


def _load_index() -> dict:
    idx = _index_file()
    if not os.path.exists(idx):
        return {}
    try:
        with open(idx, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ── Public API ────────────────────────────────────────────────────────────────

def _append_history(username: str, user_id, record: dict) -> None:
    """Append one download record to this user's history."""
    data = _load_user_data(username, user_id)
    data.setdefault("history", []).append(record)
    _atomic_write(_user_file(username, user_id), data)


def _save_message_record(
    message_id: str, username: str, user_id, record: dict
) -> None:
    """Save button data to user file + register message_id in the index."""
    data = _load_user_data(username, user_id)
    data.setdefault("registry", {})[str(message_id)] = record
    _atomic_write(_user_file(username, user_id), data)

    index = _load_index()
    index[str(message_id)] = {"username": str(username), "user_id": str(user_id)}
    _atomic_write(_index_file(), index)


def _get_message_record(message_id: str) -> dict | None:
    """Look up button data for a message_id via the index (works after restart)."""
    index = _load_index()
    entry = index.get(str(message_id))
    if not entry:
        return None
    data = _load_user_data(entry["username"], entry["user_id"])
    return data.get("registry", {}).get(str(message_id))


def _remove_message_record(message_id: str) -> None:
    """Clean up a user's registry entry and the index when a message is deleted."""
    index = _load_index()
    entry = index.pop(str(message_id), None)
    if entry:
        data = _load_user_data(entry["username"], entry["user_id"])
        data.get("registry", {}).pop(str(message_id), None)
        _atomic_write(_user_file(entry["username"], entry["user_id"]), data)
        _atomic_write(_index_file(), index)


# ══════════════════════════════════════════════════════════════════════════════
# ❓ Confirmation View
# ══════════════════════════════════════════════════════════════════════════════
class ConfirmDeleteView(discord.ui.View):
    def __init__(self, requester_id: int, messages_to_delete: list, message_id: str = ""):
        super().__init__(timeout=10800)
        self.requester_id       = requester_id
        self.messages_to_delete = messages_to_delete
        self.message_id         = message_id

    @discord.ui.button(label="Yes, delete it! 🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (interaction.user.id != self.requester_id
                and not interaction.permissions.manage_messages):
            return await interaction.response.send_message(
                "Sorry 🥺, But you must run the command (/download-music) to interact with it, Okay? 💖️",
                ephemeral=True
            )
        await interaction.response.edit_message(
            content="✨ *Poof!* The music has been deleted! ☁️", view=None
        )
        await asyncio.gather(
            *[msg.delete() for msg in self.messages_to_delete if msg],
            return_exceptions=True
        )
        if self.message_id:
            await asyncio.to_thread(_remove_message_record, self.message_id)

    @discord.ui.button(label="Cancel 🌸", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="✨ Cancelled! The music stays. 🎧", view=None
        )


# ══════════════════════════════════════════════════════════════════════════════
# 🗑️ Delete / Album Art View  —  PERSISTENT (survives bot restarts)
# ══════════════════════════════════════════════════════════════════════════════
# Rules:
#   1. timeout=None
#   2. Every button has a hard-coded unique custom_id
#   3. bot.add_view(DeleteMusicView()) called once in cog_load
#   4. Runtime data (requester_id, video_id, title) looked up from the
#      user's  *_interaction.json  via message_index.json on every click
# ─────────────────────────────────────────────────────────────────────────────
class DeleteMusicView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="music_dl:delete"
    )
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        record       = await asyncio.to_thread(_get_message_record, str(interaction.message.id))
        requester_id = int(record["requester_id"]) if record else None
        is_owner     = requester_id and interaction.user.id == requester_id
        is_mod       = interaction.permissions.manage_messages

        if is_owner or is_mod:
            view = ConfirmDeleteView(
                requester_id or interaction.user.id,
                [interaction.message],
                str(interaction.message.id)
            )
            await interaction.response.send_message(
                "🌸 Are you sure you want to delete this track?",
                view=view, ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ **Oh no! 🥺** Only the person who requested this can delete it, Sorry! 😭",
                ephemeral=True
            )

    @discord.ui.button(
        label="Show Album Art",
        style=discord.ButtonStyle.secondary,
        emoji="🖼️",
        custom_id="music_dl:album_art"
    )
    async def show_album_art(self, interaction: discord.Interaction, button: discord.ui.Button):
        record   = await asyncio.to_thread(_get_message_record, str(interaction.message.id))
        video_id = record.get("video_id", "") if record else ""
        title    = record.get("title", "Unknown") if record else "Unknown"
        artist   = record.get("artist", "Unknown Artist") if record else "Unknown Artist"

        if not video_id:
            return await interaction.response.send_message(
                "⚠️ No album art available for this track.", ephemeral=True
            )

        img_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
        # ── Random pastel embed color 🎨 ─────────────────────────────────────
        embed_color = random.choice([
                0xFFC0CB,  # Pink 🌸
                0xB57EDC,  # Lavender 💜
                0xFFD1DC,  # Blush 🌷
                0xAEC6CF,  # Pastel Blue 🩵
                0xB5EAD7,  # Mint 🍃
                0xFFDAB9,  # Peach 🍑
                0xFFF0A0,  # Butter Yellow 🌼
                0xC9C0D3,  # Lilac 🫧
                0xFFB7CE,  # Baby Pink 💖
                0xA8D8EA,  # Sky Blue ☁️
                0xFDFD96,  # Pastel Yellow 🍯
                0xE0BBE4,  # Soft Purple 🦄
                0x957DAD,  # Muted Berry 🍇
                0xD4F0F0,  # Pale Cyan 🧊
                0xFFE5B4,  # Apricot 🍹
                0xE2F0CB,  # Light Sage 🌿
                0xFFCCF9,  # Cotton Candy 🍬
                0xC5E1A5,  # Tea Green 🍵
                0xF4978E,  # Coral Pink 🦢
                0xB8E1FF,  # Dreamy Blue 🧚‍♂️
                0xFFE4E1,  # Misty Rose 🍧
                0xE6E6FA,  # Periwinkle 🎐
                0xFADADD,  # Pale Pink 🍨
                0xAFEEEE,  # Pale Turquoise 💎
                0xFFF9B1,  # Chiffon Yellow 🐤
                0xDBD7FB,  # Cloud Purple ☁️
                0xB2F2BB,  # Pastel Green 🍃
                0xFFD8B1,  # Soft Orange 🍊
                0xD0F0C0,  # Tea 🍵
                0xC1E1C1,  # Sage Green 🌵
                0xF0EAD6,  # Eggshell 🥚
                0xFFD1B3,  # Melon 🍉
                0xE9FFDB,  # Nyan Green 🧶
                0xCFE2F3,  # Ice Blue ❄️
                0xDEB887,  # Burly Wood 🪵
                0xFFC3A0,  # Pink Orange 🌅
                0xD1D1D1,  # Silver 🐘
                0xFBC4AB,  # Apricot Pink 🍑
                0xFFEBCD,  # Almond 🥜
                0xC3B1E1,  # Pastel Violet 🔮
            ])

        art_embed = discord.Embed(title=f"🖼️ Album Art For {artist} — {title}", color=embed_color)
        art_embed.set_image(url=img_url)
        await interaction.response.send_message(embed=art_embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# 📩 DM Notification View  —  shown in channel after a successful DM delivery
# Two buttons:
#   • "Open DM 📩"  — URL button linking directly to the user's DM channel
#   • "OK ✅"       — deletes this notification from the channel
# Persistent — survives bot restarts via JSON lookup (same pattern as
# DeleteMusicView). requester_id is read from the message registry on click.
# ══════════════════════════════════════════════════════════════════════════════
class DmNotificationView(discord.ui.View):
    def __init__(self, dm_channel_id: int = 0, requester_id: int = 0):
        super().__init__(timeout=None)   # persistent — never expires
        self.requester_id = requester_id

        # URL button is dynamic — only added on first creation, not on
        # re-registration (dm_channel_id == 0). Discord keeps the rendered
        # URL in the message itself so re-registered views don't need to
        # re-add it; they only need to handle the OK custom_id.
        if dm_channel_id:
            self.add_item(discord.ui.Button(
                label    = "Open DM 📩",
                style    = discord.ButtonStyle.link,
                url      = f"https://discord.com/channels/@me/{dm_channel_id}",
                row      = 0,
            ))

    @discord.ui.button(
        label     = "OK ✅",
        style     = discord.ButtonStyle.success,
        custom_id = "music_dl:dm_ok",
        row       = 0,
    )
    async def ok_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Fetch requester_id from JSON (works after restarts)
        record       = await asyncio.to_thread(_get_message_record, str(interaction.message.id))
        requester_id = int(record["requester_id"]) if record else self.requester_id

        if interaction.user.id != requester_id:
            return await interaction.response.send_message(
                "🥺 Only the person who used /download-music can dismiss this!",
                ephemeral=True,
            )
        await interaction.message.delete()
        # Clean up the JSON record now that the message is gone
        await asyncio.to_thread(_remove_message_record, str(interaction.message.id))


# ══════════════════════════════════════════════════════════════════════════════
# 🎵 Music Downloader Cog
# ══════════════════════════════════════════════════════════════════════════════
class MusicDownloader(commands.Cog):
    def __init__(self, bot):
        self.bot     = bot
        self.session: aiohttp.ClientSession = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            if self.session is not None:
                logger.warning("🔄 aiohttp session was closed — reconnecting...")
            self.session = aiohttp.ClientSession()
            logger.info("✅ aiohttp session (re)created.")
        return self.session

    async def cog_load(self):
        self.bot.add_view(DeleteMusicView())      # ✨ re-register persistent views on startup
        self.bot.add_view(DmNotificationView())   # ✨ handles OK button after restarts
        await self._ensure_session()
        logger.info("🌸 MusicDownloader Cog loaded successfully!")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("☁️ MusicDownloader Cog unloaded.")

    # ── Autocomplete ──────────────────────────────────────────────────────────
    async def search_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        if not current or ytm is None:
            return []
        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None, lambda: ytm.search(current, filter="songs", limit=7)
            )
            choices = []
            for res in results:
                title    = res.get('title', 'Unknown')
                artist   = ", ".join([a['name'] for a in res.get('artists', [])])
                video_id = res.get('videoId')
                if video_id:
                    choices.append(app_commands.Choice(
                        name=f"🎵 {title} - {artist}"[:100],
                        value=f"https://www.youtube.com/watch?v={video_id}"
                    ))
            return choices
        except Exception:
            return []

    # ── /download-music ───────────────────────────────────────────────────────
    @app_commands.command(name="download-music", description="Download an Audio from YouTube with yt-dlp! 🎨🎵")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        search="Type a song name or paste a link ✨",
        send_to="Where should I send your audio? 📬",
        format="Select your file format 🍓",
        quality="Choose kbps quality (Max = Highest source) ☁️"
    )
    @app_commands.choices(send_to=[
        app_commands.Choice(name="📢 Current Channel (Default)", value="channel"),
        app_commands.Choice(name="📩 Send to my DM privately",   value="dm"),
    ])
    @app_commands.choices(format=[
        app_commands.Choice(name="M4A / AAC 🍎 (Recommended! it's really fast! 🌟)",             value="m4a"),
        app_commands.Choice(name="MP3 🎵 (May be Slow 😒, But good for compatibility approx ~50s ✨️)",                   value="mp3"),        
        app_commands.Choice(name="Opus 🎤 (Moderate 👍)",                  value="opus"),
    ])
    @app_commands.choices(quality=[
        app_commands.Choice(name="64 kbps (Tiny 🐣)",            value="64"),
        app_commands.Choice(name="128 kbps (Light ☁️)",          value="128"),
        app_commands.Choice(name="192 kbps (Standard 🎀)",       value="192"),
        app_commands.Choice(name="256 kbps (High 💫)",           value="256"),
        app_commands.Choice(name="Max YouTube Limit (Ultra 👑)", value="0"),
    ])
    @app_commands.autocomplete(search=search_autocomplete)
    async def music(
        self,
        interaction: discord.Interaction,
        search: str,
        send_to: str = "channel",
        format: str = "m4a",
        quality: str = "192"
    ):
        logger.info(
            f"🎵 CMD [/download-music] | User: {interaction.user} ({interaction.user.id}) "
            f"| Query: '{search}' | Format: {format} | Quality: {quality} | Send To: {send_to}"
        )

        channel = interaction.channel
        if channel is None:
            err_embed = discord.Embed(
                title="❌ Channel Error",
                description="Cannot determine the channel. Please try again.",
                color=0xFF6B6B
            )
            return await interaction.response.send_message(embed=err_embed, ephemeral=True)

        # ── Loading embed — sent as the interaction's first response ────────────
        _lc = random.choice([
            0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF, 0xB5EAD7,
            0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xFFB7CE, 0xA8D8EA,
            0xFDFD96, 0xE0BBE4, 0x957DAD, 0xD4F0F0, 0xFFE5B4,
        ])
        _loading_embed = discord.Embed(
            title       = "🎶 Preparing your audio...",
            description = (
                "Polishing your masterpiece! ✨️\n\n"
                "This usually takes 30–90s depending on the track! ⏰️\n\n"
                "So prepare your coffee! 😊☕️"
            ),
            color = _lc,
        )
        _loading_embed.set_thumbnail(url=random.choice([
            "https://c.tenor.com/knwWU-EgRmMAAAAC/tenor.gif",
            "https://c.tenor.com/J9mOaXMbKygAAAAC/tenor.gif",
            "https://c.tenor.com/plvrL3peoBIAAAAC/tenor.gif",
            "https://c.tenor.com/Yo4Vo-XCgqEAAAAC/tenor.gif",
            "https://c.tenor.com/ts-81PaXp3AAAAAC/tenor.gif",
            "https://c.tenor.com/Ly_w3cT7B04AAAAC/tenor.gif",
        ]))
        await interaction.response.send_message(embed=_loading_embed)

        temp_id = f"enchanted_{interaction.user.id}_{str(interaction.id)[-5:]}"

        # ── yt-dlp options (Enchanted Speed Edition 🚀) ───────────────────────
        ydl_opts = {
            # ── Speed & Engine ────────────────────────────────────────────────
            # Using aria2c to open 16 simultaneous connections for max speed!
            'external_downloader': 'aria2c',
            'external_downloader_args': [
                '--min-split-size=1M',
                '--max-connection-per-server=16',
                '--split=16',
                '--max-overall-download-limit=0',
                '--console-log-level=warn'
            ],

            # ── Anti-Throttle ─────────────────────────────────────────────────
            # Prioritize iOS/Android clients to bypass the 50-80KiB/s desktop cap
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'android', 'web'],
                    'player_skip':   ['webpage'],
                }
            },

            # Use 1MB chunks to keep the download steady 
            'http_chunk_size': 1024 * 1024, 
            
            # ── Format & Output ───────────────────────────────────────────────
            'format':            'bestaudio/best',
            'outtmpl':           f'temp/{temp_id}.%(ext)s',
            'retries':           10,
            'fragment_retries':  10,
            'retry_sleep_functions': {'http': lambda n: min(2 ** n, 30)},
            'writethumbnail':    True,
            'quiet':             True,
            'no_warnings':       True,
            'postprocessors': [
                {'key': 'FFmpegMetadata', 'add_metadata': True},
                {'key': 'EmbedThumbnail', 'already_have_thumbnail': False},
            ],
        }

        if format != "original":
            is_max = quality == "0"
            ydl_opts['postprocessors'].insert(0, {
                'key':              'FFmpegExtractAudio',
                'preferredcodec':   format,
                'preferredquality': quality if not is_max else '320',
            })
            hw_args = (
                FAST_ENCODE_ARGS + HW_ENCODE_ARGS
                if format in HW_SUPPORTED_FORMATS
                else FAST_ENCODE_ARGS
            )
            ydl_opts['postprocessor_args'] = {'FFmpegExtractAudio': hw_args}


        loading_msg     = None
        pretty_filename = None

        # Shorthand for the requester — used in filenames + records
        req_user = interaction.user
        req_name = str(req_user)
        req_id   = req_user.id

        try:
            await self._ensure_session()

            # ── Download ──────────────────────────────────────────────────────
            download_start = time.monotonic()
            loop           = asyncio.get_running_loop()
            info           = await loop.run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(search, download=True)
            )

            if info and 'entries' in info:
                entries = [e for e in info['entries'] if e]
                if not entries:
                    raise ValueError("yt-dlp returned no usable entries for this query.")
                info = entries[0]
            if not info:
                raise ValueError("yt-dlp returned no info. The track may be unavailable.")

            title      = info.get('title', 'Unknown Title')
            artist     = info.get('artist') or info.get('uploader') or 'Unknown Artist'
            video_id   = info.get('id', '')
            actual_ext = format if format != "original" else info.get('ext', 'webm')

            # ── Timing & duration ─────────────────────────────────────────────
            elapsed_secs   = time.monotonic() - download_start
            elapsed_str    = (
                f"{int(elapsed_secs // 60)}m {int(elapsed_secs % 60)}s"
                if elapsed_secs >= 60 else f"{elapsed_secs:.1f}s"
            )
            raw_duration   = info.get('duration') or 0
            dur_h, dur_rem = divmod(int(raw_duration), 3600)
            dur_m, dur_s   = divmod(dur_rem, 60)
            duration_str   = (
                (f"{dur_h}:{dur_m:02d}:{dur_s:02d}" if dur_h else f"{dur_m}:{dur_s:02d}")
                if raw_duration else "Unknown"
            )

            clean_name = f"{artist} - {title} [{video_id}]"
            for char in '<>:"/\\|?*\n\r\t':
                clean_name = clean_name.replace(char, '')
            clean_name      = clean_name[:180]
            pretty_filename = f"temp/{temp_id}_{clean_name}.{actual_ext}"

            # ── Locate downloaded file ────────────────────────────────────────
            downloaded_path = None
            for ext in [actual_ext, 'mp3', 'm4a', 'opus', 'ogg', 'webm', 'wav', 'flac', 'aac']:
                test_path = f"temp/{temp_id}.{ext}"
                if os.path.exists(test_path):
                    downloaded_path = test_path
                    break

            if not downloaded_path:
                err_embed = discord.Embed(
                    title="❌ File Not Found",
                    description="Audio file not found after download. The conversion may have failed. ☁️",
                    color=0xFF6B6B
                )
                err_embed.set_footer(text="Error code: E002")
                return await interaction.edit_original_response(embed=err_embed)

            os.rename(downloaded_path, pretty_filename)
            file_size = os.path.getsize(pretty_filename) / (1024 * 1024)

            if file_size > 24.8:
                try: os.remove(pretty_filename)
                except Exception: pass
                err_embed = discord.Embed(
                    title="⚠️ File Too Large",
                    description=(
                        f"**{file_size:.1f} MB** — Discord's limit is **25 MB** 🍓\n\n"
                        "Try a lower quality or a shorter track."
                    ),
                    color=0xFFB347
                )
                err_embed.set_footer(text="Error code: E003")
                return await interaction.edit_original_response(embed=err_embed)

            # ── Build embed & view ────────────────────────────────────────────
            del_view        = DeleteMusicView()
            display_quality = "Source" if quality == '0' else f"{quality} kbps"

            # ✨ Pick a random "enchanted" color for the success embed
            enchanted_color = random.choice([
                0xFFC0CB,  # Pink 🌸
                0xB57EDC,  # Lavender 💜
                0xFFD1DC,  # Blush 🌷
                0xAEC6CF,  # Pastel Blue 🩵
                0xB5EAD7,  # Mint 🍃
                0xFFDAB9,  # Peach 🍑
                0xFFF0A0,  # Butter Yellow 🌼
                0xC9C0D3,  # Lilac 🫧
                0xFFB7CE,  # Baby Pink 💖
                0xA8D8EA,  # Sky Blue ☁️
                0xFDFD96,  # Pastel Yellow 🍯
                0xE0BBE4,  # Soft Purple 🦄
                0x957DAD,  # Muted Berry 🍇
                0xD4F0F0,  # Pale Cyan 🧊
                0xFFE5B4,  # Apricot 🍹
                0xE2F0CB,  # Light Sage 🌿
                0xFFCCF9,  # Cotton Candy 🍬
                0xC5E1A5,  # Tea Green 🍵
                0xF4978E,  # Coral Pink 🦢
                0xB8E1FF,  # Dreamy Blue 🧚‍♂️
                0xFFE4E1,  # Misty Rose 🍧
                0xE6E6FA,  # Periwinkle 🎐
                0xFADADD,  # Pale Pink 🍨
                0xAFEEEE,  # Pale Turquoise 💎
                0xFFF9B1,  # Chiffon Yellow 🐤
                0xDBD7FB,  # Cloud Purple ☁️
                0xB2F2BB,  # Pastel Green 🍃
                0xFFD8B1,  # Soft Orange 🍊
                0xD0F0C0,  # Tea 🍵
                0xC1E1C1,  # Sage Green 🌵
                0xF0EAD6,  # Eggshell 🥚
                0xFFD1B3,  # Melon 🍉
                0xE9FFDB,  # Nyan Green 🧶
                0xCFE2F3,  # Ice Blue ❄️
                0xDEB887,  # Burly Wood 🪵
                0xFFC3A0,  # Pink Orange 🌅
                0xD1D1D1,  # Silver 🐘
                0xFBC4AB,  # Apricot Pink 🍑
                0xFFEBCD,  # Almond 🥜
                0xC3B1E1,  # Pastel Violet 🔮
            ])

            # Apply the random color to the title bar! 🎨
            success_embed = discord.Embed(title="ℹ️ Track Information:", color=enchanted_color)
            success_embed.add_field(name="🎶 Song Title", value=title,           inline=True)
            success_embed.add_field(name="🎙 Artist",     value=artist,          inline=True)
            success_embed.add_field(name="⏱️ Duration",   value=duration_str,    inline=True)
            success_embed.add_field(name="📄 Format",     value=format.upper(),  inline=True)
            success_embed.add_field(name="🔊 Quality",    value=display_quality, inline=True)
            success_embed.add_field(name="📥 Took",       value=elapsed_str,     inline=True)
            success_embed.set_footer(
                text="💡 Tip: To Download the Audio, Press and hold the file then scroll down → Save File 😊🙏"
            )

            # ── Send — edit loading in-place with file + embed ─────────────────
            greeting = (
                f"Heyyyyy there!! {req_user.mention} {random.choice(GREETING_EMOJI)} , "
                f"**Your Audio is Ready! {random.choice(READY_EMOJI)}**"
            )

            if send_to == "dm":
                try:
                    combined_msg = await req_user.send(
                        content = greeting,
                        file    = discord.File(pretty_filename, filename=f"{clean_name}.{actual_ext}"),
                        embed   = success_embed,
                        view    = del_view,
                    )
                    dm_notif_view = DmNotificationView(
                        dm_channel_id = combined_msg.channel.id,
                        requester_id  = req_id,
                    )
                    notif_msg = await interaction.edit_original_response(
                        embed=discord.Embed(
                            title       = "📩 Sent to your DM!",
                            description = "Check your Direct Messages for your audio! 🌸",
                            color       = 0xB5EAD7,
                        ),
                        view=dm_notif_view,
                    )
                    # Persist so OK button survives restarts
                    await asyncio.to_thread(
                        _save_message_record,
                        str(notif_msg.id), req_name, req_id,
                        {
                            "requester_id":  req_id,
                            "dm_channel_id": combined_msg.channel.id,
                            "type":          "dm_notification",
                        }
                    )
                except discord.Forbidden:
                    # DMs are closed — edit loading in-place with the file
                    combined_msg = await interaction.edit_original_response(
                        content     = greeting,
                        embed       = success_embed,
                        attachments = [discord.File(pretty_filename, filename=f"{clean_name}.{actual_ext}")],
                        view        = del_view,
                    )
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title       = "⚠️ Couldn't DM You",
                            description = (
                                "Your DMs are closed so I sent it to the channel instead! 😊\n"
                                "Enable DMs from server members to use this option."
                            ),
                            color = 0xFFB347,
                        ),
                        ephemeral=True,
                    )
            else:
                # Edit the loading embed in-place — file replaces it 🎶
                combined_msg = await interaction.edit_original_response(
                    content     = greeting,
                    embed       = success_embed,
                    attachments = [discord.File(pretty_filename, filename=f"{clean_name}.{actual_ext}")],
                    view        = del_view,
                )

            # ── Persist to user's interaction file ───────────────────────────
            # 1. Button registry (for Delete / Album Art after restart)
            await asyncio.to_thread(
                _save_message_record,
                str(combined_msg.id), req_name, req_id,
                {
                    "requester_id": req_id,
                    "video_id":     video_id,
                    "title":        title,
                    "artist":       artist,                    
                }
            )
            # 2. Download history
            await asyncio.to_thread(
                _append_history, req_name, req_id,
                {
                    "timestamp":    datetime.now().isoformat(),
                    "status":       "success",
                    "query":        search,
                    "title":        title,
                    "artist":       artist,
                    "video_id":     video_id,
                    "format":       format,
                    "quality":      display_quality,
                    "duration":     duration_str,
                    "file_size_mb": round(file_size, 2),
                    "elapsed":      elapsed_str,
                    "guild_id":     str(interaction.guild_id or "DM"),
                    "guild_name":   interaction.guild.name if interaction.guild else "DM",
                    "channel_id":   str(interaction.channel_id),
                    "message_id":   str(combined_msg.id),
                }
            )

            logger.info(
                f"✅ Delivered '{title}' ({file_size:.1f} MB) "
                f"to {req_user} ({req_id}) in {elapsed_str}"
            )

        except Exception as e:
            logger.error(
                f"❌ Error for {req_user} | query='{search}'\n{traceback.format_exc()}"
            )

            # Save error to user's history too
            await asyncio.to_thread(
                _append_history, req_name, req_id,
                {
                    "timestamp":  datetime.now().isoformat(),
                    "status":     "error",
                    "query":      search,
                    "format":     format,
                    "quality":    quality,
                    "error":      str(e)[:300],
                    "guild_id":   str(interaction.guild_id or "DM"),
                }
            )
            err_code = _err_code(e)
            try:
                err_embed = discord.Embed(
                    title="❌ Something went wrong",
                    description=f"`{str(e)[:200]}`\n\nPlease try again or use a different link. ☁️",
                    color=0xFF6B6B
                )
                err_embed.set_footer(
                    text=f"Error code: {err_code}  •  Try a different quality or format 😊"
                )
                await interaction.edit_original_response(embed=err_embed)
            except Exception:
                pass

        finally:
            if pretty_filename and os.path.exists(pretty_filename):
                try: os.remove(pretty_filename)
                except Exception: pass
            for ext in ['mp3', 'm4a', 'wav', 'opus', 'ogg', 'webm', 'flac', 'aac',
                        'temp', 'jpg', 'jpeg', 'png', 'webp']:
                path = f"temp/{temp_id}.{ext}"
                if os.path.exists(path):
                    try: os.remove(path)
                    except Exception: pass


async def setup(bot):
    await bot.add_cog(MusicDownloader(bot))
