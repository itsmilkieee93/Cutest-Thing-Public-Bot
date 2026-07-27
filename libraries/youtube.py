import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import logging
import random
import isodate
import json
import os
import re
import sys
from datetime import datetime, timedelta

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config


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


# ══════════════════════════════════════════════════════════════════════════════
# 💾 PER-USER INTERACTION STORE  (mirrors music_downloader.py exactly)
# ══════════════════════════════════════════════════════════════════════════════
# File layout:
#   interactions/
#     StayHalalBro_123456_yt_interaction.json  ← per-user history + button registry
#     yt_message_index.json                    ← message_id → {username, user_id}
#
# Kept separate from music_downloader's message_index.json to avoid schema
# collisions between the two modules.
# ─────────────────────────────────────────────────────────────────────────────
INTERACTIONS_DIR = "interactions"
os.makedirs(INTERACTIONS_DIR, exist_ok=True)


def _clean_name(username: str) -> str:
    """Sanitise a Discord username for safe use as part of a filename."""
    return re.sub(r'[^\w]', '_', str(username))[:24].strip('_') or 'user'


def _user_file(username: str, user_id) -> str:
    return os.path.join(
        INTERACTIONS_DIR,
        f"{_clean_name(username)}_{user_id}_yt_interaction.json"
    )


def _index_file() -> str:
    return os.path.join(INTERACTIONS_DIR, "yt_message_index.json")


def _atomic_write(path: str, data: dict | list) -> None:
    """Write JSON atomically so a crash mid-write never corrupts data."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"❌ Write failed [{path}]: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


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
    logger.info(f"✅ Saved yt record — msg:{message_id} user:{username}")


def _get_message_record(message_id: str) -> dict | None:
    """Look up button data for a message_id via the index (works after restart)."""
    index = _load_index()
    entry = index.get(str(message_id))
    if not entry:
        return None
    data = _load_user_data(entry["username"], entry["user_id"])
    return data.get("registry", {}).get(str(message_id))


def _append_history(username: str, user_id, record: dict) -> None:
    """Append one search record to the user's history."""
    data = _load_user_data(username, user_id)
    data.setdefault("history", []).append(record)
    _atomic_write(_user_file(username, user_id), data)


# ══════════════════════════════════════════════════════════════════════════════
# 🕐 Duration formatting helper
# ══════════════════════════════════════════════════════════════════════════════
def _fmt_duration(duration) -> str:
    if isinstance(duration, timedelta):
        total = int(duration.total_seconds())
    else:
        total = int(duration.totimedelta(start=datetime.utcnow()).total_seconds())

    total = max(0, total)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)

    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


# ══════════════════════════════════════════════════════════════════════════════
# 🎬 YouTube Button View — persistent across restarts
# ══════════════════════════════════════════════════════════════════════════════
class YouTubeView(discord.ui.View):
    def __init__(self, video_url: str):
        super().__init__(timeout=None)

        self.add_item(discord.ui.Button(
            label="View Video",
            url=video_url,
            style=discord.ButtonStyle.link,
            emoji="📲"
        ))

    @discord.ui.button(
        label="View Description",
        style=discord.ButtonStyle.secondary,
        emoji="📄",
        custom_id="yt:view_description"
    )
    async def view_description(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        responded = interaction.response.is_done()
        if not responded:
            try:
                await interaction.response.defer(ephemeral=True)
                responded = True
            except discord.errors.NotFound:
                return
            except Exception:
                responded = False

        async def _reply(embed: discord.Embed):
            if responded:
                try:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
                except Exception:
                    pass
            try:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception:
                pass

        try:
            if interaction.message is None:
                return await _reply(discord.Embed(
                    title="❌ Message Unavailable",
                    description="Couldn't retrieve the message. Please run `/youtube` again! 😔",
                    color=0xFF6B6B
                ))

            # ── Look up via per-user index (same pattern as music_downloader) ──
            record = await asyncio.to_thread(
                _get_message_record, str(interaction.message.id)
            )

            if not record:
                return await _reply(discord.Embed(
                    title="❌ Data Not Found",
                    description=(
                        "This video's data couldn't be found. 😔\n"
                        "Please run `/youtube` again to get fresh results!"
                    ),
                    color=0xFF6B6B
                ))

            desc  = record.get("description", "").strip()
            title = record.get("title", "Unknown")
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

    def _get_api_key(self):
        key = (key_config.YOUTUBE_API_KEY or "").strip()
        return key or None

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

    @app_commands.command(name="youtube", description="Get video/playlist metadata via YouTube API v3! ⚡")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Paste a link or search for something...")
    @app_commands.autocomplete(query=yt_autocomplete)
    async def yt_universal(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        req_user = interaction.user
        req_name = str(req_user)
        req_id   = str(req_user.id)

        api_key = self._get_api_key()
        if not api_key:
            return await interaction.followup.send("❌ Missing API Key in `key_config.YOUTUBE_API_KEY`!")

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
            search_url = "https://www.googleapis.com/youtube/v3/search"
            s_params   = {'part': 'snippet', 'q': q, 'type': 'video', 'maxResults': 1, 'key': api_key}
            async with self.bot.session.get(search_url, params=s_params) as s_resp:
                s_data   = await s_resp.json()
                video_id = s_data.get('items', [{}])[0].get('id', {}).get('videoId')
                if video_id:
                    target_url = f"https://www.youtube.com/watch?v={video_id}"
                else:
                    return await interaction.followup.send("❌ No videos found for that search query.")

        try:
            ui_color = random.choice(PASTEL_COLORS)
            embed    = discord.Embed(color=ui_color)
            snippet  = None

            # ── CASE 1: VIDEO / SHORT ─────────────────────────────────────────
            if video_id:
                view = YouTubeView(video_url=target_url)

                v_url    = "https://www.googleapis.com/youtube/v3/videos"
                v_params = {'part': 'snippet,statistics,contentDetails', 'id': video_id, 'key': api_key}
                async with self.bot.session.get(v_url, params=v_params) as v_resp:
                    v_data = await v_resp.json()
                    if not v_data.get('items'):
                        return await interaction.followup.send("❌ Video not found or is unavailable.")

                    item    = v_data['items'][0]
                    snippet = item['snippet']
                    stats   = item['statistics']

                    date_obj     = datetime.strptime(snippet['publishedAt'], "%Y-%m-%dT%H:%M:%SZ")
                    pub_date     = date_obj.strftime("%A, %d %B %Y | %H:%M:%S (UTC+0)")
                    raw_duration = isodate.parse_duration(item['contentDetails']['duration'])
                    duration_str = _fmt_duration(raw_duration)
                    is_short     = "/shorts/" in q

                    embed.title  = f"{'📱 Short' if is_short else '🎥 Video'}: {snippet['title']}"

                    likes_val    = f"{int(stats['likeCount']):,}"    if 'likeCount'    in stats else 'Hidden'
                    comments_val = f"{int(stats['commentCount']):,}" if 'commentCount' in stats else 'Disabled'

                    embed.add_field(name="👤 Publisher",      value=snippet['channelTitle'],               inline=True)
                    embed.add_field(name="📅 Date Published", value=pub_date,                              inline=True)
                    embed.add_field(name="⏱️ Duration",       value=duration_str,                          inline=True)
                    embed.add_field(name="👀 Views",           value=f"{int(stats.get('viewCount', 0)):,}", inline=True)
                    embed.add_field(name="👍 Likes",           value=likes_val,                             inline=True)
                    embed.add_field(name="💬 Comments",        value=comments_val,                          inline=True)
                    embed.add_field(name="🆔 Video ID",        value=video_id,                              inline=True)

                    thumb = (
                        snippet['thumbnails'].get('maxres', {}).get('url')
                        or snippet['thumbnails'].get('high',   {}).get('url')
                    )
                    embed.set_image(url=thumb)

            # ── CASE 2: PLAYLIST ──────────────────────────────────────────────
            elif playlist_id:
                view = discord.ui.View()

                p_url    = "https://www.googleapis.com/youtube/v3/playlists"
                p_params = {'part': 'snippet,contentDetails', 'id': playlist_id, 'key': api_key}
                async with self.bot.session.get(p_url, params=p_params) as p_resp:
                    p_data = await p_resp.json()
                    if not p_data.get('items'):
                        return await interaction.followup.send("❌ Playlist not found or is unavailable.")

                    item    = p_data['items'][0]
                    snippet = item['snippet']

                    date_obj = datetime.strptime(snippet['publishedAt'], "%Y-%m-%dT%H:%M:%SZ")
                    pub_date = date_obj.strftime("%A, %d %B %Y | %H:%M:%S (UTC+0)")

                    embed.title = f"📂 Playlist: {snippet['title']}"
                    embed.add_field(name="👤 Creator",      value=snippet['channelTitle'],                  inline=True)
                    embed.add_field(name="📅 Created On",   value=pub_date,                                 inline=True)
                    embed.add_field(name="📦 Total Videos", value=str(item['contentDetails']['itemCount']), inline=True)
                    embed.add_field(name="🔗 Link",          value=f"[Open Playlist]({target_url}) ✨",      inline=True)

                    embed.set_image(url=(
                        snippet['thumbnails'].get('maxres', {}).get('url')
                        or snippet['thumbnails'].get('high',   {}).get('url')
                        or snippet['thumbnails'].get('medium', {}).get('url', '')
                    ))
                    view.add_item(discord.ui.Button(
                        label="View Playlist",
                        url=target_url,
                        style=discord.ButtonStyle.link,
                        emoji="📂"
                    ))
            else:
                return await interaction.followup.send("❌ Could not determine video or playlist ID.")

            embed.set_footer(
                text="YouTube ✨️",
                icon_url=(
                    "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e7/"
                    "YouTube_social_white_squircle_%282024%29.svg/"
                    "1280px-YouTube_social_white_squircle_%282024%29.svg.png"
                )
            )

            # ── Send (wait=True so we have the message ID immediately) ─────────
            sent_msg = await interaction.followup.send(embed=embed, view=view, wait=True)

            # ── Persist per-user record (same pattern as music_downloader) ─────
            if video_id and sent_msg and snippet:
                registry_record = {
                    "title":       snippet['title'],
                    "description": snippet.get('description', ''),
                    "color":       ui_color,
                    "video_id":    video_id,
                    "channel":     snippet.get('channelTitle', ''),
                    "timestamp":   datetime.now().isoformat(),
                }
                # 1. Button registry + index (for View Description after restart)
                await asyncio.to_thread(
                    _save_message_record,
                    str(sent_msg.id), req_name, req_id, registry_record
                )
                # 2. Search history
                await asyncio.to_thread(
                    _append_history, req_name, req_id,
                    {
                        "timestamp":  datetime.now().isoformat(),
                        "query":      query,
                        "title":      snippet['title'],
                        "channel":    snippet.get('channelTitle', ''),
                        "video_id":   video_id,
                        "message_id": str(sent_msg.id),
                        "guild_id":   str(interaction.guild_id or "DM"),
                        "guild_name": interaction.guild.name if interaction.guild else "DM",
                    }
                )

        except Exception as e:
            logger.error(f"❌ yt_universal error for {req_user}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: `{str(e)[:200]}`")


async def setup(bot):
    await bot.add_cog(YouTubeUniversalModule(bot))
    bot.add_view(YouTubeView(video_url="https://youtube.com"))
    print("✅ YouTube Module loaded successfully!")
