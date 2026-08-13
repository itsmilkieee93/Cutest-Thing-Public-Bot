# encoding: utf-8
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import random
import logging
import os
import sys
import json
import re
from datetime import datetime

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

# ─── Logger ───────────────────────────────────────────────────────────────────
os.makedirs('log', exist_ok=True)
logger = logging.getLogger('Pexels')
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(
    'log/pexels.log', encoding='utf-8'
)
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
    _stdout_handler.setFormatter(logging.Formatter('[Pexels] [%(levelname)s] %(message)s'))
    logger.addHandler(_stdout_handler)

# ─── Data Directory ───────────────────────────────────────────────────────────
DATA_DIR          = "data/pexels"
MESSAGES_FILE     = os.path.join(DATA_DIR, "pexels_messages.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ─── Pastel Colors ────────────────────────────────────────────────────────────
PASTEL_COLORS = [
    0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF, 0xB5EAD7,
    0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xFFB7CE, 0xA8D8EA,
    0xFDFD96, 0xE0BBE4, 0x957DAD, 0xD4F0F0, 0xFFE5B4,
    0xE2F0CB, 0xFFCCF9, 0xC5E1A5, 0xF4978E, 0xB8E1FF,
]

# ══════════════════════════════════════════════════════════════════════════════
# 📷 Pexels Cog
# ══════════════════════════════════════════════════════════════════════════════
class PexelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot        = bot
        self.api_base   = "https://api.pexels.com"

    def _get_headers(self) -> dict | None:
        """Pexels uses a bare API key — no Bearer or Client-ID prefix."""
        key = (key_config.PEXELS_API_KEY or "").strip()
        if key:
            return {
                "Authorization": key,
                "Accept": "application/json",
            }
        return None

    # ── Shared error helper ────────────────────────────────────────────────────
    async def _send_error(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        color: int = 0xFF6B6B,
    ):
        embed = discord.Embed(title=title, description=description, color=color)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Storage helper ─────────────────────────────────────────────────────────
    def _save_interaction(
        self,
        interaction: discord.Interaction,
        record: dict,
    ):
        """Append record to pexels_messages.json and the per-user JSON file."""
        user     = interaction.user
        username = re.sub(r'[^\w.-]', '_', str(user.name))   # filesystem-safe
        user_id  = user.id
        ts       = datetime.now().isoformat(timespec="seconds")

        entry = {
            "timestamp":  ts,
            "user_id":    user_id,
            "username":   str(user.name),
            "guild_id":   interaction.guild_id,
            "channel_id": interaction.channel_id,
            **record,
        }

        # ── pexels_messages.json (global log) ─────────────────────────────────
        try:
            try:
                with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
                    messages = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                messages = []

            messages.append(entry)

            with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write {MESSAGES_FILE}: {e}")

        # ── {username}_{user_id}_pexels.json (per-user) ───────────────────────
        user_file = os.path.join(DATA_DIR, f"{username}_{user_id}_pexels.json")
        try:
            try:
                with open(user_file, "r", encoding="utf-8") as f:
                    user_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                user_data = {
                    "user_id":   user_id,
                    "username":  str(user.name),
                    "total_uses": 0,
                    "history":   [],
                }

            user_data["total_uses"] += 1
            user_data["username"]    = str(user.name)   # keep display name fresh
            user_data["history"].append(entry)

            with open(user_file, "w", encoding="utf-8") as f:
                json.dump(user_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write {user_file}: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # Slash command group: /pexels
    # ══════════════════════════════════════════════════════════════════════════
    pexels = app_commands.Group(
        name="pexels",
        description="Fetch stunning content from Pexels! 📷✨",
        guild_only=True,
    )

    # ── /pexels photo ──────────────────────────────────────────────────────────
    @pexels.command(
        name="photo",
        description="Fetch a random photo from Pexels! 🖼️✨"
    )
    @app_commands.describe(
        query       = "Search for a specific topic (e.g. nature, city, cats) 🔍",
        orientation = "Photo orientation 📐",
    )
    @app_commands.choices(orientation=[
        app_commands.Choice(name="🖼️  Any Orientation",  value="any"),
        app_commands.Choice(name="🌄  Landscape",         value="landscape"),
        app_commands.Choice(name="🖼️  Portrait",         value="portrait"),
        app_commands.Choice(name="⬛  Square",            value="square"),
    ])
    async def photo(
        self,
        interaction: discord.Interaction,
        query:       str = None,
        orientation: str = "any",
    ):
        await interaction.response.defer()

        headers = self._get_headers()
        if not headers:
            return await self._send_error(
                interaction,
                "❌ API Key Missing",
                "Pexels API key not found!\nMake sure `key_config.PEXELS_API_KEY` is set. 🔑",
            )

        # ── Build request ──────────────────────────────────────────────────────
        try:
            async with aiohttp.ClientSession() as session:
                if query:
                    # Search endpoint
                    params = {
                        "query":    query.strip(),
                        "per_page": 1,
                        "page":     random.randint(1, 100),
                    }
                    if orientation != "any":
                        params["orientation"] = orientation
                    url = f"{self.api_base}/v1/search"
                else:
                    # Curated endpoint (truly random)
                    params = {
                        "per_page": 1,
                        "page":     random.randint(1, 1000),
                    }
                    url = f"{self.api_base}/v1/curated"

                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    remaining = resp.headers.get("X-Ratelimit-Remaining", "?")

                    if resp.status == 401:
                        body = await resp.text()
                        logger.error(f"401 Unauthorized — body: {body[:300]}")
                        return await self._send_error(
                            interaction,
                            "❌ Unauthorized",
                            "Your Pexels API key is invalid or expired. 🔑\n"
                            "Check the value of `key_config.PEXELS_API_KEY`.",
                        )

                    if resp.status == 429:
                        return await self._send_error(
                            interaction,
                            "⚠️ Rate Limited",
                            "You've hit the Pexels API rate limit. Try again later! 😔",
                            color=0xFFB347,
                        )

                    if resp.status != 200:
                        return await self._send_error(
                            interaction,
                            "❌ API Error",
                            f"Pexels returned status `{resp.status}`. Please try again! 😔",
                        )

                    data = await resp.json()

        except aiohttp.ClientError as e:
            logger.error(f"Connection error: {e}")
            return await self._send_error(
                interaction,
                "❌ Connection Error",
                f"Couldn't reach Pexels API.\n```{str(e)[:150]}```",
            )

        # ── Parse ──────────────────────────────────────────────────────────────
        try:
            photos = data.get("photos", [])
            if not photos:
                return await self._send_error(
                    interaction,
                    "🔍 No Photos Found",
                    f"Couldn't find any photos{f' for **{query}**' if query else ''}. "
                    "Try a different topic! 😊",
                    color=0xFFB347,
                )

            photo       = photos[0]
            photo_id    = photo["id"]
            alt         = photo.get("alt") or "A beautiful photo"
            alt         = alt.capitalize()[:200]
            photo_url   = photo["url"]                    # Pexels page link
            image_url   = photo["src"]["large2x"]         # embed image
            full_url    = photo["src"]["original"]        # full-res download
            width       = photo.get("width", 0)
            height      = photo.get("height", 0)
            photographer      = photo.get("photographer", "Unknown")
            photographer_url  = photo.get("photographer_url")

            # Dominant color → embed color
            avg_color = photo.get("avg_color")
            if avg_color:
                try:
                    embed_color = int(avg_color.lstrip("#"), 16)
                except ValueError:
                    embed_color = random.choice(PASTEL_COLORS)
            else:
                embed_color = random.choice(PASTEL_COLORS)

        except (KeyError, TypeError, IndexError) as e:
            logger.error(f"Parse error: {e} | data: {str(data)[:300]}")
            return await self._send_error(
                interaction,
                "❌ Parse Error",
                "Received an unexpected response from Pexels. Please try again! 😔",
            )

        # ── Build embed ────────────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"📷 {alt}",
            color=embed_color,
            timestamp=datetime.now(),
        )
        embed.set_image(url=image_url)

        embed.add_field(
            name="📸 Photographer",
            value=f"[{photographer}]({photographer_url})" if photographer_url else photographer,
            inline=True,
        )
        embed.add_field(
            name="📐 Resolution",
            value=f"`{width} × {height}`",
            inline=True,
        )
        embed.add_field(
            name="🔑 Requests Left",
            value=f"`{remaining}` / month",
            inline=True,
        )
        if query:
            embed.add_field(
                name="🔍 Search Query",
                value=f"`{query}`",
                inline=False,
            )

        embed.set_author(name=photographer)
        embed.set_footer(
            text=f"Pexels  •  Photo ID: {photo_id}",
            icon_url="https://www.pexels.com/favicon.ico",
        )

        # ── Buttons ────────────────────────────────────────────────────────────
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(
            label="View on Pexels",
            url=photo_url,
            style=discord.ButtonStyle.link,
            emoji="🔗",
        ))
        view.add_item(discord.ui.Button(
            label="Full Resolution",
            url=full_url,
            style=discord.ButtonStyle.link,
            emoji="🖼️",
        ))

        await interaction.followup.send(embed=embed, view=view)

        self._save_interaction(interaction, {
            "type":         "photo",
            "query":        query,
            "orientation":  orientation if orientation != "any" else None,
            "photo_id":     photo_id,
            "photo_url":    photo_url,
            "image_url":    image_url,
            "photographer": photographer,
            "photographer_url": photographer_url,
            "resolution":   f"{width}x{height}",
            "alt":          alt,
        })

        logger.info(
            f"📷 CMD [/pexels photo] | User: {interaction.user} ({interaction.user.id}) "
            f"| Query: '{query}' | Photo ID: {photo_id} | Photographer: {photographer}"
        )

    # ── /pexels video ──────────────────────────────────────────────────────────
    @pexels.command(
        name="video",
        description="Fetch a random video from Pexels! 🎬✨"
    )
    @app_commands.describe(
        query = "Search for a specific topic (e.g. ocean, timelapse, dogs) 🔍",
    )
    async def video(
        self,
        interaction: discord.Interaction,
        query: str = None,
    ):
        await interaction.response.defer()

        headers = self._get_headers()
        if not headers:
            return await self._send_error(
                interaction,
                "❌ API Key Missing",
                "Pexels API key not found!\nMake sure `key_config.PEXELS_API_KEY` is set. 🔑",
            )

        # ── Build request ──────────────────────────────────────────────────────
        try:
            async with aiohttp.ClientSession() as session:
                if query:
                    url    = f"{self.api_base}/videos/search"
                    params = {
                        "query":    query.strip(),
                        "per_page": 1,
                        "page":     random.randint(1, 50),
                    }
                else:
                    url    = f"{self.api_base}/videos/popular"
                    params = {
                        "per_page": 1,
                        "page":     random.randint(1, 100),
                    }

                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    remaining = resp.headers.get("X-Ratelimit-Remaining", "?")

                    if resp.status == 401:
                        body = await resp.text()
                        logger.error(f"401 Unauthorized — body: {body[:300]}")
                        return await self._send_error(
                            interaction,
                            "❌ Unauthorized",
                            "Your Pexels API key is invalid or expired. 🔑\n"
                            "Check the value of `key_config.PEXELS_API_KEY`.",
                        )

                    if resp.status == 429:
                        return await self._send_error(
                            interaction,
                            "⚠️ Rate Limited",
                            "You've hit the Pexels API rate limit. Try again later! 😔",
                            color=0xFFB347,
                        )

                    if resp.status != 200:
                        return await self._send_error(
                            interaction,
                            "❌ API Error",
                            f"Pexels returned status `{resp.status}`. Please try again! 😔",
                        )

                    data = await resp.json()

        except aiohttp.ClientError as e:
            logger.error(f"Connection error: {e}")
            return await self._send_error(
                interaction,
                "❌ Connection Error",
                f"Couldn't reach Pexels API.\n```{str(e)[:150]}```",
            )

        # ── Parse ──────────────────────────────────────────────────────────────
        try:
            videos = data.get("videos", [])
            if not videos:
                return await self._send_error(
                    interaction,
                    "🔍 No Videos Found",
                    f"Couldn't find any videos{f' for **{query}**' if query else ''}. "
                    "Try a different topic! 😊",
                    color=0xFFB347,
                )

            vid         = videos[0]
            vid_id      = vid["id"]
            vid_url     = vid["url"]                      # Pexels page link
            width       = vid.get("width", 0)
            height      = vid.get("height", 0)
            duration    = vid.get("duration", 0)
            thumbnail   = vid.get("image")                # preview thumbnail

            user        = vid.get("user", {})
            videographer     = user.get("name", "Unknown")
            videographer_url = user.get("url")

            # Pick best video file (prefer HD, fallback to first)
            files = vid.get("video_files", [])
            hd_files = [f for f in files if f.get("quality") in ("hd", "sd")]
            best_file = hd_files[0] if hd_files else (files[0] if files else None)
            direct_url = best_file["link"] if best_file else None

        except (KeyError, TypeError, IndexError) as e:
            logger.error(f"Parse error: {e} | data: {str(data)[:300]}")
            return await self._send_error(
                interaction,
                "❌ Parse Error",
                "Received an unexpected response from Pexels. Please try again! 😔",
            )

        # ── Build embed ────────────────────────────────────────────────────────
        embed_color = random.choice(PASTEL_COLORS)
        mins, secs  = divmod(duration, 60)
        duration_fmt = f"{mins}:{secs:02d}"

        embed = discord.Embed(
            title=f"🎬 Pexels Video",
            color=embed_color,
            timestamp=datetime.now(),
        )
        if thumbnail:
            embed.set_image(url=thumbnail)

        embed.add_field(
            name="🎥 Videographer",
            value=f"[{videographer}]({videographer_url})" if videographer_url else videographer,
            inline=True,
        )
        embed.add_field(
            name="📐 Resolution",
            value=f"`{width} × {height}`",
            inline=True,
        )
        embed.add_field(
            name="⏱️ Duration",
            value=f"`{duration_fmt}`",
            inline=True,
        )
        embed.add_field(
            name="🔑 Requests Left",
            value=f"`{remaining}` / month",
            inline=True,
        )
        if query:
            embed.add_field(
                name="🔍 Search Query",
                value=f"`{query}`",
                inline=False,
            )

        embed.set_footer(
            text=f"Pexels  •  Video ID: {vid_id}",
            icon_url="https://www.pexels.com/favicon.ico",
        )

        # ── Buttons ────────────────────────────────────────────────────────────
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(
            label="View on Pexels",
            url=vid_url,
            style=discord.ButtonStyle.link,
            emoji="🔗",
        ))
        if direct_url:
            view.add_item(discord.ui.Button(
                label="Direct Download",
                url=direct_url,
                style=discord.ButtonStyle.link,
                emoji="📥",
            ))

        await interaction.followup.send(embed=embed, view=view)

        self._save_interaction(interaction, {
            "type":            "video",
            "query":           query,
            "video_id":        vid_id,
            "video_url":       vid_url,
            "thumbnail_url":   thumbnail,
            "direct_url":      direct_url,
            "videographer":    videographer,
            "videographer_url": videographer_url,
            "resolution":      f"{width}x{height}",
            "duration_secs":   duration,
        })

        logger.info(
            f"🎬 CMD [/pexels video] | User: {interaction.user} ({interaction.user.id}) "
            f"| Query: '{query}' | Video ID: {vid_id} | Videographer: {videographer}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PexelsCog(bot))
    print("✅ Pexels Module loaded! 📷✨")
