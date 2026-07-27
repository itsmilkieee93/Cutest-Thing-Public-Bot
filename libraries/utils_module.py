import discord
from discord import app_commands
from discord.ext import commands
import utils 
import os

class UtilsModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="upload", description="Send a file! 📄")
    @app_commands.describe(file="Select a file to send! ✨")
    async def upload(self, interaction: discord.Interaction, file: discord.Attachment):
        # 1. 🤫 Defer EPHEMERALLY! 
        # This makes the "thinking" state and the progress bar visible ONLY to you.
        await interaction.response.defer(ephemeral=True)
        
        # 2. 📢 Send to the PUBLIC chat immediately!
        # Because the interaction is private, we use channel.send for the public file.
        await interaction.channel.send(
            content=f" ",
            file=await file.to_file()
        )
        
        # 3. 📥 Run the private upload tracker to save it to ./downloads
        await utils.save_to_downloads(interaction, file)

async def setup(bot):
    await bot.add_cog(UtilsModule(bot))
