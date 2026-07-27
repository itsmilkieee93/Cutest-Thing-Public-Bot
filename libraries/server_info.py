import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import asyncio
from datetime import datetime, timezone

DISCORD_EPOCH_MS = 1420070400000

VERIFICATION_LEVELS   = {0: "None", 1: "Low", 2: "Medium", 3: "High", 4: "Highest"}
CONTENT_FILTER_LEVELS = {0: "Disabled", 1: "Members without roles", 2: "All members"}
NOTIFICATION_LEVELS   = {0: "All Messages", 1: "Only @mentions"}
MFA_LEVELS            = {0: "Not required", 1: "Required"}
NSFW_LEVELS           = {0: "Default", 1: "Explicit", 2: "Safe", 3: "Age Restricted"}
PREMIUM_TIER_NAMES    = {0: "None", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}


class ServerInfoCog(commands.Cog):
    """
    🌸 /server-info — pulls the FULL guild object straight from Discord's v10
    REST API (raw aiohttp, no discord.py Guild cache) and renders every field
    worth showing as one metadata embed. Server name = title, server icon =
    thumbnail (top-right box), banner (if any) = big image underneath.
    """

    def __init__(self, bot):
        self.bot     = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    # ── Discord v10 REST fetching (raw aiohttp) ─────────────────────────────
    async def _fetch_guild_json(self, guild_id: int) -> dict | None:
        session = await self._get_session()
        url     = f"https://discord.com/api/v10/guilds/{guild_id}"
        headers = {"Authorization": f"Bot {self.bot.http.token}"}
        params  = {"with_counts": "true"}

        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 429:
                    data        = await resp.json()
                    retry_after = float(data.get("retry_after", 1))
                    await asyncio.sleep(retry_after + 0.1)
                    return await self._fetch_guild_json(guild_id)
                if resp.status != 200:
                    print(f"⚠️ [server-info] fetch returned {resp.status}")
                    return None
                return await resp.json()
        except Exception as e:
            print(f"⚠️ [server-info] fetch error: {e}")
            return None

    # ── metadata helpers ─────────────────────────────────────────────────────
    def _icon_url(self, guild_id, icon_hash: str | None) -> str | None:
        if not icon_hash:
            return None
        ext = "gif" if icon_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{ext}?size=256"

    def _banner_url(self, guild_id, banner_hash: str | None) -> str | None:
        if not banner_hash:
            return None
        ext = "gif" if banner_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/banners/{guild_id}/{banner_hash}.{ext}?size=1024"

    def _splash_url(self, guild_id, splash_hash: str | None) -> str | None:
        if not splash_hash:
            return None
        return f"https://cdn.discordapp.com/splashes/{guild_id}/{splash_hash}.png?size=1024"

    def _created_at(self, guild_id: str) -> tuple[str, str]:
        """🌸 Snowflake → ("Tuesday, 07 July 2026", "14:23:05") in UTC."""
        timestamp_ms = (int(guild_id) >> 22) + DISCORD_EPOCH_MS
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        return dt.strftime("%A, %d %B %Y"), dt.strftime("%H:%M:%S")

    # ── command ──────────────────────────────────────────────────────────────
    @app_commands.command(name="server-info", description="Fetches full server metadata")
    async def server_info(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        if interaction.guild_id is None:
            await interaction.followup.send("😭 This command only works inside a server!")
            return

        data = await self._fetch_guild_json(interaction.guild_id)
        if not data:
            await interaction.followup.send("😭 Couldn't fetch this server's metadata from Discord's API!")
            return

        guild_id    = data.get("id")
        name        = data.get("name", "Unknown Server")
        icon_url    = self._icon_url(guild_id, data.get("icon"))
        banner_url  = self._banner_url(guild_id, data.get("banner"))
        splash_url  = self._splash_url(guild_id, data.get("splash"))
        date_str, time_str = self._created_at(guild_id)

        # 🌸 Discord retired the guild-wide voice "region" field years ago —
        # it's now per-channel (rtc_region) instead. Some older API payloads
        # still carry it, so we show it if present and fall back gracefully.
        region = data.get("region")

        owner_id      = data.get("owner_id")
        members       = data.get("approximate_member_count")
        online        = data.get("approximate_presence_count")
        boost_tier    = data.get("premium_tier", 0)
        boost_count   = data.get("premium_subscription_count", 0)
        verification  = data.get("verification_level", 0)
        content_filter = data.get("explicit_content_filter", 0)
        mfa_level     = data.get("mfa_level", 0)
        notifications = data.get("default_message_notifications", 0)
        nsfw_level    = data.get("nsfw_level", 0)
        locale        = data.get("preferred_locale", "en-US")
        emojis        = data.get("emojis", [])
        roles         = data.get("roles", [])
        stickers      = data.get("stickers", [])
        features      = data.get("features", [])
        vanity_code   = data.get("vanity_url_code")
        description   = data.get("description")
        widget_on     = data.get("widget_enabled", False)
        progress_bar  = data.get("premium_progress_bar_enabled", False)
        max_members   = data.get("max_members")
        max_presences = data.get("max_presences")
        max_video     = data.get("max_video_channel_users")

        system_channel  = data.get("system_channel_id")
        rules_channel   = data.get("rules_channel_id")
        updates_channel = data.get("public_updates_channel_id")
        afk_channel     = data.get("afk_channel_id")
        afk_timeout     = data.get("afk_timeout", 0)

        embed = discord.Embed(
            title=f"{name}",
            color=0xFF6EC7,  # 🌸 neon pink brand color
            timestamp=discord.utils.utcnow(),
        )
        if icon_url:
            embed.set_thumbnail(url=icon_url)  # 🖼️ pfp box, top-right
        # 🌸 The banner always renders as the big image at the very bottom of
        # the embed. If the server has no banner but does have an invite
        # splash, use that instead so there's still a bottom visual.
        if banner_url:
            embed.set_image(url=banner_url)
        elif splash_url:
            embed.set_image(url=splash_url)

        embed.add_field(name="👑 Owner", value=f"<@{owner_id}>", inline=True)
        embed.add_field(name="🆔 Server ID", value=str(guild_id), inline=True)
        embed.add_field(name="📅 Created", value=f"{date_str}\n{time_str} UTC", inline=True)

        embed.add_field(name="👥 Members", value=f"{members:,}" if members is not None else "Unknown", inline=True)
        embed.add_field(name="🟢 Online", value=f"{online:,}" if online is not None else "Unknown", inline=True)
        embed.add_field(
            name="🚀 Boosts",
            value=f"{PREMIUM_TIER_NAMES.get(boost_tier, boost_tier)} • {boost_count} boosts",
            inline=True,
        )

        embed.add_field(
            name="🛡️ Safety",
            value=(
                f"Verification: {VERIFICATION_LEVELS.get(verification, verification)}\n"
                f"Content Filter: {CONTENT_FILTER_LEVELS.get(content_filter, content_filter)}\n"
                f"2FA for Mods: {MFA_LEVELS.get(mfa_level, mfa_level)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="🔔 Defaults",
            value=(
                f"Notifications: {NOTIFICATION_LEVELS.get(notifications, notifications)}\n"
                f"NSFW Level: {NSFW_LEVELS.get(nsfw_level, nsfw_level)}\n"
                f"Locale: {locale}"
            ),
            inline=True,
        )
        embed.add_field(
            name="🌍 Region",
            value=region.replace("-", " ").title() if region else "Automatic (per-channel)",
            inline=True,
        )
        embed.add_field(
            name="🎭 Server Assets",
            value=f"Roles: {len(roles)}\nEmojis: {len(emojis)}\nStickers: {len(stickers)}",
            inline=True,
        )

        limits_lines = []
        if max_members is not None:
            limits_lines.append(f"Max Members: {max_members:,}")
        if max_presences is not None:
            limits_lines.append(f"Max Presences: {max_presences:,}")
        if max_video is not None:
            limits_lines.append(f"Max Video Users: {max_video}")
        limits_lines.append(f"Widget: {'Enabled' if widget_on else 'Disabled'}")
        limits_lines.append(f"Boost Progress Bar: {'On' if progress_bar else 'Off'}")
        embed.add_field(name="📶 Limits & Extras", value="\n".join(limits_lines), inline=True)

        channel_lines = []
        if system_channel:
            channel_lines.append(f"📢 System: <#{system_channel}>")
        if rules_channel:
            channel_lines.append(f"📜 Rules: <#{rules_channel}>")
        if updates_channel:
            channel_lines.append(f"📣 Public Updates: <#{updates_channel}>")
        if afk_channel:
            channel_lines.append(f"🎙️ AFK: <#{afk_channel}> ({afk_timeout // 60} min timeout)")
        if channel_lines:
            embed.add_field(name="📌 Key Channels", value="\n".join(channel_lines), inline=False)

        if features:
            feature_text = ", ".join(f.replace("_", " ").title() for f in sorted(features))
            embed.add_field(name="📋 Features", value=feature_text[:1024], inline=False)

        if vanity_code:
            embed.add_field(name="🔗 Vanity URL", value=f"discord.gg/{vanity_code}", inline=True)

        if banner_url and splash_url:
            embed.add_field(name="🖼️ Invite Splash", value=f"[View splash image]({splash_url})", inline=True)

        if description:
            embed.add_field(name="📝 Description", value=description[:1024], inline=False)

        embed.set_footer(text="Server Metadata")

        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ServerInfoCog(bot))
