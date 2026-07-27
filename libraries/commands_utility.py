import discord
from discord import app_commands
from resources.shared import bridge_log


def setup_commands(bot):

    # =====================================================================
    # 🔍 COMMAND: /safs
    # =====================================================================
    @bot.tree.command(name="safs", description="Safety Check! 🛡")
    async def safs(interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("🛡️ **Safety Protocol:** Nominal.")
            await bridge_log(interaction, "safs", "Safety Protocol", "Nominal")
        except:
            pass

    # =====================================================================
    # ⭐️ COMMAND: /unf
    # =====================================================================
    @bot.tree.command(name="unf", description="Unfold! ⭐️")
    async def unf(interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("🌬️ **UNF:** Processed.")
            await bridge_log(interaction, "unf", "UNF Command", "Processed")
        except:
            pass
