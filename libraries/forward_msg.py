import discord
from discord import app_commands
from discord.ext import commands
from typing import Union
import os


class ForwardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.whitelist_path = "auth/whitelist"

    def get_allowed_servers(self):
        if not os.path.exists(self.whitelist_path): return []
        try:
            if os.path.getsize(self.whitelist_path) == 0: return []
            with open(self.whitelist_path, "r") as f:
                return [int(line.strip()) for line in f if line.strip().isdigit()]
        except: return []

    async def server_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = []
        for guild in self.bot.guilds:
            if current.lower() in guild.name.lower():
                choices.append(app_commands.Choice(name=guild.name, value=str(guild.id)))
        return choices[:25]

    async def channel_autocomplete(self, interaction: discord.Interaction, current: str):
        server_id_str = interaction.namespace.to_server
        if not server_id_str: return []
        try:
            guild = self.bot.get_guild(int(server_id_str))
            if not guild: return []
            choices = []
            for channel in guild.text_channels:
                if current.lower() in channel.name.lower():
                    choices.append(app_commands.Choice(name=f"#{channel.name}", value=str(channel.id)))
            return choices[:25]
        except: return []

    async def message_id_autocomplete(self, interaction: discord.Interaction, current: str):
        from_channel = interaction.namespace.from_channel
        if not from_channel: return []
        try:
            import re
            cid = from_channel.id if hasattr(from_channel, 'id') else int(from_channel)
            channel = self.bot.get_channel(cid)
            if not channel: return []

            def clean(text: str) -> str:
                # Strip all Discord mentions — roles, users, channels, everyone, here
                text = re.sub(r'<@&\d+>', '', text)   # role mentions
                text = re.sub(r'<@!?\d+>', '', text)  # user mentions
                text = re.sub(r'<#\d+>', '', text)    # channel mentions
                text = re.sub(r'@(everyone|here)', '', text)
                # Strip custom emojis <:name:id> and animated <a:name:id>
                text = re.sub(r'<a?:\w+:\d+>', '', text)
                # Strip markdown bold/italic/code
                text = re.sub(r'[*_`~|]', '', text)
                return text.strip()

            choices = []
            async for msg in channel.history(limit=20):
                # 🌸 Try content first, then embed, then attachment, then sticker
                if msg.content:
                    raw = clean(msg.content)
                    if not raw:  # content was only mentions
                        if msg.embeds:
                            e = msg.embeds[0]
                            parts = [p for p in [e.title, e.description] if p]
                            raw = " — ".join(parts) or "[Embed]"
                        elif msg.attachments:
                            raw = f"[{', '.join(a.filename for a in msg.attachments)}]"
                        else:
                            raw = "[Mention only]"
                elif msg.embeds:
                    e = msg.embeds[0]
                    parts = [p for p in [e.title, e.description] if p]
                    raw = " — ".join(parts) or "[Embed]"
                elif msg.attachments:
                    raw = f"[{', '.join(a.filename for a in msg.attachments)}]"
                elif msg.stickers:
                    raw = f"[Sticker: {msg.stickers[0].name}]"
                else:
                    raw = "[Empty Message]"

                snippet = (raw[:80] + '…') if len(raw) > 80 else raw
                label = f"{msg.author.display_name}: {snippet}"
                if current.lower() in label.lower() or current in str(msg.id):
                    choices.append(app_commands.Choice(name=label[:100], value=str(msg.id)))
            return choices[:25]
        except: return []

    @app_commands.command(name="forward-msg", description="Forward natively with server/channel targeting ✨")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)    
    @app_commands.describe(
        from_channel="Source channel",
        message_id="Message to forward",
        to_server="Target server",
        to_channel="Target channel (Filtered by server)",
        to_user="Or forward to a DM"
    )
    @app_commands.autocomplete(to_server=server_autocomplete, to_channel=channel_autocomplete, message_id=message_id_autocomplete)
    async def forward_msg(
        self, 
        interaction: discord.Interaction, 
        # 🎀 Trick: Use discord.abc.GuildChannel to satisfy the "entirely channels" rule!
        from_channel: discord.abc.GuildChannel, 
        message_id: str, 
        to_server: str = None,
        to_channel: str = None, 
        to_user: discord.User = None
    ):
        try: 
            await interaction.response.defer(ephemeral=True)
        except: 
            return 

        allowed_servers = self.get_allowed_servers()
        # 🛡️ Only check restrictions if the command is run INSIDE a server
        if interaction.guild_id and allowed_servers and interaction.guild_id not in allowed_servers:
            # Cute Access Denied Embed
            error_embed = discord.Embed(
                title="🚫 Access Restricted",
                description="This server isn't on the allowed list! 🥺",
                color=0xff5c5c
            )
            return await interaction.followup.send(embed=error_embed, ephemeral=True)

        # 🎯 Resolve Target
        final_target = None
        if to_user:
            final_target = to_user
        elif to_channel:
            final_target = self.bot.get_channel(int(to_channel))

        if not final_target:
            warning_embed = discord.Embed(
                title="❌ Target Missing",
                description="Please provide a target (DM or Server + Channel)! 😭",
                color=0xffb3c1
            )
            return await interaction.followup.send(embed=warning_embed, ephemeral=True)

        try:
            # 🛰️ Fetching & Forwarding
            source_msg = await from_channel.fetch_message(int(message_id))
            await source_msg.forward(final_target)
            
            # Success Embed
            success_embed = discord.Embed(
                title="✅ Message Forwarded!",
                description=f"Successfully sent to **{final_target}**! ✨",
                color=0x03fff7 # Your signature cyan
            )
            success_embed.set_footer(text="Mission accomplished! <3")
            await interaction.followup.send(embed=success_embed, ephemeral=True)

        except Exception as e:
            # 🔥 THE ERROR EMBED
            error_embed = discord.Embed(
                title="🐞 Oops! An error occurred",
                description=f"I couldn't forward that message. 🥺\n\n**Error details:**\n`{str(e)[:100]}`",
                color=0xff4f4f,
                timestamp=datetime.now()
            )
            error_embed.set_footer(text="Check the message ID and try again! 🧸")
            await interaction.followup.send(embed=error_embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(ForwardCog(bot))
