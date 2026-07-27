"""
🌸 System Commands — owner-only slash commands for bot maintenance.
Kept as a Cog (not a bare method on EnchantedBot) since app_commands
needs a Cog to bind `self` correctly at invocation time.
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime

from config_loader import get_owner_id


class SystemCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == get_owner_id()

    @app_commands.command(name="reload", description="🔄 Reload all bot modules & sync commands (owner-only)")
    async def reload_slash(self, interaction: discord.Interaction):
        """🌸 Reload all modules, re-register cogs, and sync commands."""
        if not self._is_owner(interaction):
            return await interaction.response.send_message(
                "🚫 Only my owner can use this command! 🥺", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            summary = await self.bot.reload_all_modules()
            await self.bot.tree.sync()

            n_ok  = len(summary["modules_reloaded"])
            n_bad = len(summary["modules_failed"])
            status = f"✅ **{n_ok} modules reloaded**"
            if n_bad:
                status += f"\n⚠️ **{n_bad} failed:** `{', '.join(summary['modules_failed'])}`"

            status += "\n✅ **Commands synced!** 🌸✨"

            await interaction.followup.send(status, ephemeral=True)
            print(f"🔄 Full refresh completed via /reload by {interaction.user} at {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            await interaction.followup.send(f"❌ **Reload Failed:** `{str(e)[:150]}` 🥺", ephemeral=True)
            print(f"❌ Reload failed: {e}")


async def setup(bot):
    await bot.add_cog(SystemCommands(bot))
