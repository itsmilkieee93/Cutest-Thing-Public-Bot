"""
🔐 permissions.py
Discord cog for /permission slash command.

Checks all bot permissions in the current server using raw Discord API v10.

Flow:
  1. GET /users/@me               → resolve bot's own user ID
  2. GET /guilds/{id}/members/{bot_id} → fetch bot's assigned role IDs
  3. GET /guilds/{id}/roles       → fetch all roles + their permission bits
  4. OR together every role the bot has → effective guild permissions
  5. Optional: GET /channels/{id} → apply channel overwrites on top

Token: key_config.DISCORD_TOKEN (auth/key_config.py)
"""

import os
import sys
import aiohttp
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from resources.shared import bridge_log

# ─────────────────────────────────────────────────────────────────────────────
# Paths & constants
# ─────────────────────────────────────────────────────────────────────────────
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
_AUTH_DIR  = os.path.join(_BASE_DIR, "..", "auth")
if _AUTH_DIR not in sys.path:
    sys.path.insert(0, _AUTH_DIR)
import key_config  # lives at auth/key_config.py, gitignored — see generate_key_config.py

API_BASE   = "https://discord.com/api/v10"

# ─────────────────────────────────────────────────────────────────────────────
# Full Discord permission bit table (as of API v10 / 2024)
# ─────────────────────────────────────────────────────────────────────────────
# Each entry: (bit_shift, label, category, emoji)
_PERMISSIONS: list[tuple[int, str, str, str]] = [
    # ── General ──────────────────────────────────────────────────────────────
    (3,  "Administrator",                   "⚙️ General",  "👑"),
    (5,  "Manage Server",                   "⚙️ General",  "🛠️"),
    (4,  "Manage Channels",                 "⚙️ General",  "📋"),
    (28, "Manage Roles",                    "⚙️ General",  "🏷️"),
    (29, "Manage Webhooks",                 "⚙️ General",  "🪝"),
    (30, "Manage Expressions",              "⚙️ General",  "😄"),
    (43, "Create Expressions",              "⚙️ General",  "✏️"),
    (7,  "View Audit Log",                  "⚙️ General",  "📜"),
    (19, "View Server Insights",            "⚙️ General",  "📊"),
    (41, "View Creator Monetization",       "⚙️ General",  "💰"),
    (33, "Manage Events",                   "⚙️ General",  "📅"),
    (44, "Create Events",                   "⚙️ General",  "🗓️"),

    # ── Membership ───────────────────────────────────────────────────────────
    (0,  "Create Invite",                   "👥 Membership", "📨"),
    (1,  "Kick Members",                    "👥 Membership", "👢"),
    (2,  "Ban Members",                     "👥 Membership", "🔨"),
    (40, "Timeout Members",                 "👥 Membership", "⏱️"),
    (26, "Change Own Nickname",             "👥 Membership", "✏️"),
    (27, "Manage Nicknames",                "👥 Membership", "📝"),

    # ── Text ─────────────────────────────────────────────────────────────────
    (10, "View Channels",                   "💬 Text",     "👁️"),
    (11, "Send Messages",                   "💬 Text",     "✉️"),
    (38, "Send Messages in Threads",        "💬 Text",     "🧵"),
    (12, "Send TTS Messages",               "💬 Text",     "🔊"),
    (46, "Send Voice Messages",             "💬 Text",     "🎤"),
    (49, "Send Polls",                      "💬 Text",     "📊"),
    (13, "Manage Messages",                 "💬 Text",     "🗂️"),
    (34, "Manage Threads",                  "💬 Text",     "🧵"),
    (35, "Create Public Threads",           "💬 Text",     "📂"),
    (36, "Create Private Threads",          "💬 Text",     "🔒"),
    (14, "Embed Links",                     "💬 Text",     "🔗"),
    (15, "Attach Files",                    "💬 Text",     "📎"),
    (16, "Read Message History",            "💬 Text",     "📖"),
    (6,  "Add Reactions",                   "💬 Text",     "😍"),
    (17, "Mention @everyone",               "💬 Text",     "📣"),
    (18, "Use External Emojis",             "💬 Text",     "😎"),
    (37, "Use External Stickers",           "💬 Text",     "🪄"),
    (31, "Use Application Commands",        "💬 Text",     "🤖"),

    # ── Voice ─────────────────────────────────────────────────────────────────
    (20, "Connect",                         "🎙️ Voice",    "🔌"),
    (21, "Speak",                           "🎙️ Voice",    "🗣️"),
    (9,  "Video / Go Live",                 "🎙️ Voice",    "📹"),
    (39, "Use Activities",                  "🎙️ Voice",    "🎮"),
    (8,  "Priority Speaker",                "🎙️ Voice",    "⭐"),
    (25, "Use Voice Activity",              "🎙️ Voice",    "🎚️"),
    (42, "Use Soundboard",                  "🎙️ Voice",    "🥁"),
    (45, "Use External Sounds",             "🎙️ Voice",    "🎵"),
    (22, "Mute Members",                    "🎙️ Voice",    "🔇"),
    (23, "Deafen Members",                  "🎙️ Voice",    "🙉"),
    (24, "Move Members",                    "🎙️ Voice",    "🚚"),
    (32, "Request to Speak",                "🎙️ Voice",    "🙋"),

    # ── Apps ─────────────────────────────────────────────────────────────────
    (50, "Use External Apps",               "🧩 Apps",     "🔌"),
]

_CATEGORY_ORDER = [
    "⚙️ General",
    "👥 Membership",
    "💬 Text",
    "🎙️ Voice",
    "🧩 Apps",
]

# ─────────────────────────────────────────────────────────────────────────────
# Token helper
# ─────────────────────────────────────────────────────────────────────────────
def _load_token() -> str:
    token = (key_config.DISCORD_TOKEN or "").strip()
    if not token:
        print("⚠️ Permissions: key_config.DISCORD_TOKEN is empty!")
    return token


# ─────────────────────────────────────────────────────────────────────────────
# Permission calculation helpers
# ─────────────────────────────────────────────────────────────────────────────
def _calc_guild_perms(bot_role_ids: set[str], roles: list[dict]) -> int:
    """
    OR together the permission bits of every role the bot holds,
    including the @everyone role (which applies to all members).
    Returns effective permission integer.
    """
    effective = 0
    for role in roles:
        # @everyone role has the same id as the guild
        if role["id"] in bot_role_ids or role.get("position") == 0:
            effective |= int(role["permissions"])
    return effective


def _apply_channel_overwrites(
    base_perms:  int,
    bot_user_id: str,
    bot_role_ids: set[str],
    guild_id:    str,
    overwrites:  list[dict],
) -> int:
    """
    Apply channel permission_overwrites on top of guild base permissions.
    Discord overwrite resolution order (per API v10 docs):
      1. @everyone role deny/allow
      2. All other role deny/allow (combined)
      3. Member-specific deny/allow
    """
    # ADMINISTRATOR bypasses all channel overrides
    if base_perms & (1 << 3):
        return base_perms

    perms = base_perms

    # Step 1 — @everyone overwrite (role id == guild id)
    for ow in overwrites:
        if ow["type"] == 0 and ow["id"] == guild_id:   # type 0 = role
            perms &= ~int(ow["deny"])
            perms |=  int(ow["allow"])
            break

    # Step 2 — other role overwrites the bot has
    role_deny  = 0
    role_allow = 0
    for ow in overwrites:
        if ow["type"] == 0 and ow["id"] in bot_role_ids and ow["id"] != guild_id:
            role_deny  |= int(ow["deny"])
            role_allow |= int(ow["allow"])
    perms &= ~role_deny
    perms |=  role_allow

    # Step 3 — member-specific overwrite
    for ow in overwrites:
        if ow["type"] == 1 and ow["id"] == bot_user_id:   # type 1 = member
            perms &= ~int(ow["deny"])
            perms |=  int(ow["allow"])
            break

    return perms


def _has(perms: int, bit: int) -> bool:
    """Check if permission bit is set (also grants everything if ADMIN)."""
    admin = 1 << 3
    if perms & admin:
        return True
    return bool(perms & (1 << bit))


def _build_permission_fields(perms: int) -> list[tuple[str, str]]:
    """
    Group all permissions by category and return a list of
    (field_name, field_value) tuples ready to add to an embed.
    """
    grouped: dict[str, list[str]] = {cat: [] for cat in _CATEGORY_ORDER}

    for bit, label, category, emoji in _PERMISSIONS:
        granted = _has(perms, bit)
        mark    = "✅" if granted else "❌"
        grouped[category].append(f"{mark} {emoji} {label}")

    fields: list[tuple[str, str]] = []
    for cat in _CATEGORY_ORDER:
        lines = grouped.get(cat, [])
        if lines:
            fields.append((cat, "\n".join(lines)))

    return fields


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────
class PermissionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _session(self) -> aiohttp.ClientSession | None:
        if hasattr(self.bot, "session") and self.bot.session and not self.bot.session.closed:
            return self.bot.session
        return None

    # ── /permission ───────────────────────────────────────────────────────────
    @app_commands.command(
        name        = "permission",
        description = "🔐 Check all bot permissions in this server!",
    )
    @app_commands.describe(
        channel = "Also check channel-specific permissions for this channel 📋",
    )
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def permission(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | discord.ForumChannel = None,
    ):
        await interaction.response.defer(thinking=True, ephemeral=False)

        token = _load_token()
        if not token:
            return await interaction.followup.send(
                "❌ Token not configured! Check `key_config.DISCORD_TOKEN`.", ephemeral=True
            )

        guild_id = str(interaction.guild_id)
        headers  = {
            "Authorization": f"Bot {token}",
            "Content-Type":  "application/json",
            "User-Agent":    "DiscordBot (CutestThing, 7)",
        }

        shared  = self._session()
        session = shared or aiohttp.ClientSession()

        try:
            timeout = aiohttp.ClientTimeout(total=15)

            # ── 1. Resolve bot user ID  (GET /users/@me) ──────────────────────
            async with session.get(
                f"{API_BASE}/users/@me",
                headers = headers,
                timeout = timeout,
            ) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(
                        f"❌ Couldn't resolve bot user (`{resp.status}`).", ephemeral=True
                    )
                me_data    = await resp.json()
                bot_user_id = me_data["id"]

            # ── 2. Fetch bot's member object  (GET /guilds/{id}/members/{id}) ──
            async with session.get(
                f"{API_BASE}/guilds/{guild_id}/members/{bot_user_id}",
                headers = headers,
                timeout = timeout,
            ) as resp:
                if resp.status == 404:
                    return await interaction.followup.send(
                        "❌ Bot isn't a member of this server!", ephemeral=True
                    )
                if resp.status != 200:
                    return await interaction.followup.send(
                        f"❌ Couldn't fetch bot member object (`{resp.status}`).", ephemeral=True
                    )
                member_data  = await resp.json()
                bot_role_ids = set(member_data.get("roles", []))
                # @everyone role always included (same id as guild)
                bot_role_ids.add(guild_id)
                nick = member_data.get("nick") or me_data.get("username", "Cutest Thing")

            # ── 3. Fetch all guild roles  (GET /guilds/{id}/roles) ─────────────
            async with session.get(
                f"{API_BASE}/guilds/{guild_id}/roles",
                headers = headers,
                timeout = timeout,
            ) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(
                        f"❌ Couldn't fetch guild roles (`{resp.status}`).", ephemeral=True
                    )
                all_roles: list[dict] = await resp.json()

            # ── 4. Calculate effective guild-wide permissions ──────────────────
            guild_perms = _calc_guild_perms(bot_role_ids, all_roles)
            is_admin    = bool(guild_perms & (1 << 3))

            # ── 5. Optionally apply channel overwrites ─────────────────────────
            channel_perms   = None
            channel_obj     = None
            channel_name    = None

            if channel:
                async with session.get(
                    f"{API_BASE}/channels/{channel.id}",
                    headers = headers,
                    timeout = timeout,
                ) as resp:
                    if resp.status == 200:
                        channel_data = await resp.json()
                        overwrites   = channel_data.get("permission_overwrites", [])
                        channel_perms = _apply_channel_overwrites(
                            guild_perms,
                            bot_user_id,
                            bot_role_ids,
                            guild_id,
                            overwrites,
                        )
                        channel_name = channel_data.get("name", str(channel.id))

            # ── 6. Build guild permissions embed ──────────────────────────────
            guild_embed = discord.Embed(
                title       = "🔐 Bot Permission Report",
                description = (
                    f"**Server:** {interaction.guild.name}\n"
                    f"**Bot:** {nick}\n"
                    f"**Roles held:** {len(bot_role_ids) - 1}\n"   # -1 for @everyone
                    f"**Administrator:** {'👑 YES — all perms granted!' if is_admin else '❌ No'}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color     = 0xB57EDC,
                timestamp = datetime.now(tz=timezone.utc),
            )

            for field_name, field_value in _build_permission_fields(guild_perms):
                guild_embed.add_field(
                    name   = field_name,
                    value  = field_value,
                    inline = True,
                )

            guild_embed.set_footer(
                text     = "🌸 Fetched via Discord API v10  •  Cutest Thing ✨",
                icon_url = interaction.client.user.display_avatar.url,
            )

            embeds = [guild_embed]

            # ── 7. Build channel permissions embed (if requested) ─────────────
            if channel_perms is not None and channel_name:
                ch_is_admin = bool(channel_perms & (1 << 3))
                channel_embed = discord.Embed(
                    title       = f"📋 Channel Permissions — #{channel_name}",
                    description = (
                        f"Permissions after applying channel overwrites.\n"
                        f"**Administrator:** {'👑 YES' if ch_is_admin else '❌ No'}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    color     = 0xAEC6CF,
                    timestamp = datetime.now(tz=timezone.utc),
                )
                for field_name, field_value in _build_permission_fields(channel_perms):
                    channel_embed.add_field(
                        name   = field_name,
                        value  = field_value,
                        inline = True,
                    )
                channel_embed.set_footer(
                    text     = "🌸 Fetched via Discord API v10  •  Cutest Thing ✨",
                    icon_url = interaction.client.user.display_avatar.url,
                )
                embeds.append(channel_embed)

            await interaction.followup.send(embeds=embeds, ephemeral=True)

            await bridge_log(
                interaction, "permission",
                f"Guild: {interaction.guild.name}",
                f"Admin={is_admin} | Perms={guild_perms} | Channel={channel_name or 'N/A'}",
            )

        except aiohttp.ClientConnectorError:
            await interaction.followup.send(
                "❌ Network error — couldn't reach Discord API.", ephemeral=True
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "⏳ Request timed out. Try again!", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Unexpected error: `{str(e)[:150]}`", ephemeral=True
            )
        finally:
            if shared is None and not session.closed:
                await session.close()


# ─────────────────────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(PermissionsCog(bot))
