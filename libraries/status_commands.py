"""
🌸 Cutest Thing — Status Commands
/status-set and /status-remove — owner-only control of the bot's global
Discord presence (Playing/Watching/Listening/Streaming/Custom bubble).

Drop-in cog. Writes to the SAME status.json that EnchantedBot.sync_loop
(bot_service.py) already watches every second, so changes persist across
restarts. Also applies instantly via change_presence() for immediate
feedback instead of waiting on the loop's 1s tick.

Register in discord_commands.py:
    await bot.add_cog(StatusCommands(bot))
"""

import os
import json

import discord
from discord import app_commands
from discord.ext import commands

from config_loader import is_owner

MAX_STATUS_LEN = 128
DEFAULT_STREAM_URL = "https://twitch.tv/discord"

STATUS_TYPE_CHOICES = [
    app_commands.Choice(name="✨ Custom", value="custom"),
    app_commands.Choice(name="🎮 Playing", value="playing"),
    app_commands.Choice(name="📺 Watching", value="watching"),
    app_commands.Choice(name="🎧 Listening", value="listening"),
    app_commands.Choice(name="🔴 Streaming", value="streaming"),
]


class StatusCommands(commands.Cog):
    """🌸 Owner-only bot presence controls."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _write_status_file(self, data: dict):
        os.makedirs(self.bot.status_dir, exist_ok=True)
        with open(self.bot.status_file, "w") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _parse_emoji(emoji_str: str):
        if not emoji_str:
            return None
        try:
            return discord.PartialEmoji.from_str(emoji_str)
        except Exception:
            return None

    @app_commands.command(name="status-set", description="🌸 Set the bot's custom status/activity")
    @app_commands.describe(
        status_type="What kind of status to show",
        text="The status text (e.g. 'with kawaii bots 🌸')",
        emoji="Emoji for the bubble — custom status only (unicode or server emoji)",
        url="Stream URL — streaming status only",
    )
    @app_commands.choices(status_type=STATUS_TYPE_CHOICES)
    async def status_set(
        self,
        interaction: discord.Interaction,
        status_type: app_commands.Choice[str],
        text: str = None,
        emoji: str = None,
        url: str = None,
    ):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("oh nooo! 😭, this one's owner-only 🥺❌️", ephemeral=True)
            return

        stype = status_type.value

        if stype == "custom" and not text and not emoji:
            await interaction.response.send_message(
                "need at least `text` or `emoji` for a custom status 🌸", ephemeral=True
            )
            return
        if stype != "custom" and not text:
            await interaction.response.send_message(
                f"`text` is required for a **{stype}** status 🌸", ephemeral=True
            )
            return

        if text and len(text) > MAX_STATUS_LEN:
            text = text[:MAX_STATUS_LEN]

        parsed_emoji = self._parse_emoji(emoji)
        stream_url = url or DEFAULT_STREAM_URL

        data = {
            "type": stype,
            "bubble": text if stype == "custom" else "",
            "name": text if stype != "custom" else "",
            "emoji": emoji or "",
            "url": stream_url,
        }
        self._write_status_file(data)

        activity = None
        if stype == "custom":
            activity = discord.CustomActivity(name=text or "", emoji=parsed_emoji)
        elif stype == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=text)
        elif stype == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=text)
        elif stype == "streaming":
            activity = discord.Streaming(name=text, url=stream_url)
        elif stype == "playing":
            activity = discord.Game(name=text)

        try:
            await self.bot.change_presence(activity=activity, status=discord.Status.online)
        except Exception as e:
            print(f"⚠️ status-set: change_presence failed: {e}")

        # Keep the 1s sync_loop from redundantly re-applying the same state
        self.bot.last_state = f"{data['bubble']}|{data['type']}|{data['name']}|{data['emoji']}|{data['url']}"

        preview = f"{(emoji + ' ') if emoji else ''}{text or ''}".strip()
        embed = discord.Embed(
            title="🌸 Status updated!",
            description=f"**{status_type.name}** → {preview}",
            color=0xFF9EDB,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="status-remove", description="🌸 Clear the bot's custom status/activity")
    async def status_remove(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("oh nooo! 😭, this one's owner-only 🥺❌️", ephemeral=True)
            return

        try:
            if os.path.exists(self.bot.status_file):
                os.remove(self.bot.status_file)
        except Exception as e:
            print(f"⚠️ status-remove: failed to delete status.json: {e}")

        try:
            await self.bot.change_presence(activity=None, status=discord.Status.online)
        except Exception as e:
            print(f"⚠️ status-remove: change_presence failed: {e}")

        self.bot.last_state = None

        embed = discord.Embed(
            title="🌸 Status cleared!",
            description="Back to a plain lil online status ✨",
            color=0xFF9EDB,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusCommands(bot))
