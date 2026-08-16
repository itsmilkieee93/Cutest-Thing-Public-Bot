# encoding: utf-8
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import random
import logging
import os
import sys
from datetime import datetime

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

# ─── Logger ───────────────────────────────────────────────────────────────────
os.makedirs('log', exist_ok=True)
logger = logging.getLogger('Unsplash')
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(
    'log/unsplash.log', encoding='utf-8'
)
_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
if not logger.handlers:
    logger.addHandler(_handler)

# ─── Pastel Colors ────────────────────────────────────────────────────────────
PASTEL_COLORS = [
    0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF, 0xB5EAD7,
    0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xFFB7CE, 0xA8D8EA,
    0xFDFD96, 0xE0BBE4, 0x957DAD, 0xD4F0F0, 0xFFE5B4,
    0xE2F0CB, 0xFFCCF9, 0xC5E1A5, 0xF4978E, 0xB8E1FF,
]

# ══════════════════════════════════════════════════════════════════════════════
# 🖼️ Unsplash Cog
# ══════════════════════════════════════════════════════════════════════════════
class UnsplashCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot        = bot
        self.api_base   = "https://api.unsplash.com"

    def _get_headers(self) -> dict | None:
        """Build Authorization header.

        Priority:
          1. Client-ID (Access Key) from key_config.UNSPLASH_CLIENT_ID  ← works for all new apps
          2. Bearer token from key_config.UNSPLASH_TOKEN                ← only needed after OAuth
        """
        # ── Try Client-ID first (standard for newly created apps) ──────────────
        client_id = (key_config.UNSPLASH_CLIENT_ID or "").strip()
        if client_id:
            logger.info("Auth: using Client-ID from key_config.UNSPLASH_CLIENT_ID")
            return {
                "Authorization": f"Client-ID {client_id}",
                "Accept-Version": "v1",
            }

        # ── Fall back to Bearer (OAuth access token) ───────────────────────────
        token = (key_config.UNSPLASH_TOKEN or "").strip()
        if token:
            logger.info("Auth: using Bearer token from key_config.UNSPLASH_TOKEN")
            return {
                "Authorization": f"Bearer {token}",
                "Accept-Version": "v1",
            }

        return None

    # ── /photo ─────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="photo",
        description="Fetch a stunning random photo from Unsplash! 🖼️✨"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        query       = "Search for a specific topic (e.g. nature, city, cats) 🔍",
        orientation = "Photo orientation 📐",
    )
    @app_commands.choices(orientation=[
        app_commands.Choice(name="🖼️  Any Orientation",  value="any"),
        app_commands.Choice(name="🌄  Landscape",         value="landscape"),
        app_commands.Choice(name="🖼️  Portrait",         value="portrait"),
        app_commands.Choice(name="⬛  Square",            value="squarish"),
    ])
    async def photo(
        self,
        interaction: discord.Interaction,
        query:       str = None,
        orientation: str = "any",
    ):
        await interaction.response.defer(ephemeral=False)

        headers = self._get_headers()
        if not headers:
            err = discord.Embed(
                title="❌ API Key Missing",
                description=(
                    "Unsplash API credentials not found!\n"
                    "Make sure `key_config.UNSPLASH_CLIENT_ID` or `key_config.UNSPLASH_TOKEN` is set. 🔑"
                ),
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err, ephemeral=True)

        # ── Build params ───────────────────────────────────────────────────────
        params = {}
        if query:
            params["query"] = query.strip()
        if orientation != "any":
            params["orientation"] = orientation

        # ── Fetch from Unsplash ────────────────────────────────────────────────
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/photos/random",
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:

                    remaining = resp.headers.get("X-Ratelimit-Remaining", "?")

                    if resp.status == 401:
                        # Log the body so you can see the exact error from Unsplash
                        body = await resp.text()
                        logger.error(f"401 Unauthorized — response body: {body[:300]}")
                        err = discord.Embed(
                            title="❌ Unauthorized",
                            description=(
                                "Your Unsplash API key is invalid or expired. 🔑\n"
                                "Make sure `key_config.UNSPLASH_CLIENT_ID` contains your **Access Key** "
                                "(not the Secret Key)."
                            ),
                            color=0xFF6B6B
                        )
                        return await interaction.followup.send(embed=err, ephemeral=True)

                    if resp.status == 403:
                        err = discord.Embed(
                            title="⚠️ Rate Limited",
                            description="You've hit the Unsplash API rate limit (50 req/hour on demo). 😔",
                            color=0xFFB347
                        )
                        return await interaction.followup.send(embed=err, ephemeral=True)

                    if resp.status == 404:
                        err = discord.Embed(
                            title="🔍 No Photos Found",
                            description=(
                                f"Couldn't find any photos"
                                f"{f' for **{query}**' if query else ''}. Try a different topic! 😊"
                            ),
                            color=0xFFB347
                        )
                        return await interaction.followup.send(embed=err, ephemeral=True)

                    if resp.status != 200:
                        err = discord.Embed(
                            title="❌ API Error",
                            description=f"Unsplash returned status `{resp.status}`. Please try again! 😔",
                            color=0xFF6B6B
                        )
                        return await interaction.followup.send(embed=err, ephemeral=True)

                    data = await resp.json()

        except aiohttp.ClientError as e:
            logger.error(f"Connection error: {e}")
            err = discord.Embed(
                title="❌ Connection Error",
                description=f"Couldn't reach Unsplash API.\n```{str(e)[:150]}```",
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err, ephemeral=True)

        # ── Parse response ─────────────────────────────────────────────────────
        try:
            photo_id      = data.get("id", "unknown")
            description   = data.get("description") or data.get("alt_description") or "No description"
            description   = description.capitalize()[:200] if description else "✨ A beautiful photo"
            image_url     = data["urls"]["regular"]
            full_url      = data["urls"]["full"]
            photo_link    = data["links"]["html"]
            download_link = data["links"]["download"]
            width         = data.get("width", 0)
            height        = data.get("height", 0)
            likes         = data.get("likes", 0)
            created_at    = data.get("created_at", "")[:10]  # YYYY-MM-DD

            # Photographer info
            user          = data.get("user", {})
            photographer  = user.get("name", "Unknown")
            photo_user    = user.get("username", "")
            profile_link  = f"https://unsplash.com/@{photo_user}" if photo_user else None
            profile_pic   = user.get("profile_image", {}).get("medium")

            # Location
            location      = data.get("location", {})
            loc_name      = location.get("name") if location else None

            # Color (Unsplash provides dominant color)
            hex_color     = data.get("color", None)
            if hex_color:
                try:
                    embed_color = int(hex_color.lstrip("#"), 16)
                except ValueError:
                    embed_color = random.choice(PASTEL_COLORS)
            else:
                embed_color = random.choice(PASTEL_COLORS)

        except (KeyError, TypeError) as e:
            logger.error(f"Failed to parse Unsplash response: {e} | data: {str(data)[:300]}")
            err = discord.Embed(
                title="❌ Parse Error",
                description="Received an unexpected response from Unsplash. Please try again! 😔",
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err, ephemeral=True)

        # ── Build embed ────────────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"🖼️ {description[:200]}",
            color=embed_color,
            timestamp=datetime.now()
        )
        embed.set_image(url=image_url)

        embed.add_field(
            name="📸 Photographer",
            value=f"[{photographer}]({profile_link})" if profile_link else photographer,
            inline=True
        )
        embed.add_field(
            name="❤️ Likes",
            value=f"`{likes:,}`",
            inline=True
        )
        embed.add_field(
            name="📐 Resolution",
            value=f"`{width} × {height}`",
            inline=True
        )
        if loc_name:
            embed.add_field(
                name="📍 Location",
                value=loc_name,
                inline=True
            )
        if created_at:
            embed.add_field(
                name="📅 Published",
                value=created_at,
                inline=True
            )
        embed.add_field(
            name="🔑 API Requests Left",
            value=f"`{remaining}` / hour",
            inline=True
        )
        if query:
            embed.add_field(
                name="🔍 Search Query",
                value=f"`{query}`",
                inline=False
            )

        if profile_pic:
            embed.set_author(name=photographer, icon_url=profile_pic, url=profile_link or discord.Embed.Empty)

        embed.set_footer(
            text=f"Unsplash  •  Photo ID: {photo_id}",
            icon_url="https://unsplash.com/favicon.ico"
        )

        # ── Buttons ────────────────────────────────────────────────────────────
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(
            label="View on Unsplash",
            url=photo_link,
            style=discord.ButtonStyle.link,
            emoji="🔗"
        ))
        view.add_item(discord.ui.Button(
            label="Full Resolution",
            url=full_url,
            style=discord.ButtonStyle.link,
            emoji="🖼️"
        ))
        view.add_item(discord.ui.Button(
            label="Download",
            url=download_link,
            style=discord.ButtonStyle.link,
            emoji="📥"
        ))

        await interaction.followup.send(embed=embed, view=view)

        logger.info(
            f"🖼️ CMD [/photo] | User: {interaction.user} ({interaction.user.id}) "
            f"| Query: '{query}' | Photo ID: {photo_id} | Photographer: {photographer}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(UnsplashCog(bot))
    print("✅ Unsplash Module loaded! 🖼️✨")
