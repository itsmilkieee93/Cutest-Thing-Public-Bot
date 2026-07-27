import discord
from discord import app_commands
from discord.ext import commands
import base64

class Encryption(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 🍓 Full Mapping: Alphabet, Numbers, and Symbols
        self.emoji_map = {
            # Alphabet
            'a': '🌸', 'b': '🍓', 'c': '☁️', 'd': '🎀', 'e': '✨', 'f': '🍭',
            'g': '🌙', 'h': '🍦', 'i': '💫', 'j': '🍡', 'k': '🎐', 'l': '🍄',
            'm': '🍊', 'n': '🍑', 'o': '🦄', 'p': '🌈', 'q': '💎', 'r': '🧸',
            's': '🍯', 't': '🍬', 'u': '🍰', 'v': '🍹', 'w': '🎨', 'x': '🎸',
            'y': '🌟', 'z': '🍀',
            # Numbers
            '0': '🎈', '1': '🎠', '2': '🎁', '3': '🎡', '4': '🪁', 
            '5': '🔮', '6': '🕯️', '7': '🛸', '8': '🪐', '9': '🐾',
            # Symbols & Punctuation
            ' ': '🫧', '.': '📍', '!': '📢', '?': '❓', ',': '🎈', '@': '🛡️',
            '#': '🗝️', '$': '💸', '%': '📈', '&': '🔗', '(': '🥘', ')': '🍲',
            '-': '➖', '_': '〰️', '+': '➕', '=': '🟰', '/': 'Slash', ':': '✨'
        }
        self.reverse_map = {v: k for k, v in self.emoji_map.items()}

    def do_encrypt(self, text, mode):
        if mode == "emoji":
            return "".join([self.emoji_map.get(c.lower(), c) for c in text])
        return base64.b64encode(text.encode()).decode()

    def do_decrypt(self, code, mode):
        if mode == "emoji":
            decoded = ""
            for char in code:
                decoded += self.reverse_map.get(char, char)
            return decoded
        return base64.b64decode(code.encode()).decode()

    # Autocomplete Logic for Recent Messages
    async def message_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = []
        if interaction.channel:
            async for msg in interaction.channel.history(limit=20):
                if msg.content:
                    display_name = f"{msg.author.display_name}: {msg.content}"
                    if len(display_name) > 95:
                        display_name = display_name[:92] + "..."
                    
                    if current.lower() in display_name.lower():
                        choices.append(app_commands.Choice(name=display_name, value=str(msg.id)))
                        if len(choices) >= 25:
                            break
        return choices

    @app_commands.command(name="encrypt", description="Hide a secret message using Base64 or Emoji Ciphers! 🔒")
    @app_commands.describe(
        mode="Choose your encryption style",
        text="Type a custom message to hide",
        recent_message="OR pick a recent message from this channel",
        display_format="Send as an Embed or Plain Text?"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Secret Agent (Base64)", value="base64"),
        app_commands.Choice(name="Enchanted (Emoji)", value="emoji")
    ])
    @app_commands.choices(display_format=[
        app_commands.Choice(name="Embed 🌸", value="embed"),
        app_commands.Choice(name="Plain Text ☁️", value="plain")
    ])
    @app_commands.autocomplete(recent_message=message_autocomplete)
    async def encrypt(self, interaction: discord.Interaction, mode: str, text: str = None, recent_message: str = None, display_format: str = "embed"):
        if not text and not recent_message:
            await interaction.response.send_message("❌ **Error:** Please type a `text` OR select a `recent_message`! 🍓", ephemeral=True)
            return

        target_text = text

        if recent_message:
            try:
                msg = await interaction.channel.fetch_message(int(recent_message))
                target_text = msg.content
            except discord.NotFound:
                await interaction.response.send_message("❌ **Error:** Could not find that message anymore! ☁️", ephemeral=True)
                return

        result = self.do_encrypt(target_text, mode)
        
        if display_format == "embed":
            embed = discord.Embed(
                title="🔒 Message Encrypted",
                description=f"**Style:** `{mode.upper()}`\n\n`{result}`",
                color=0xffb7c5
            )
            embed.set_footer(text="Use /decrypt to reveal this secret! ✨")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(f"🔒 **{mode.upper()} Cipher:**\n`{result}`\n\n*Use `/decrypt` to read!* 🌸")

    @app_commands.command(name="decrypt", description="Reveal the hidden truth of a message! 🗝️")
    @app_commands.describe(
        code="The code to translate", 
        mode="The style it was hidden in",
        display_format="View as an Embed or Plain Text?"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Secret Agent (Base64)", value="base64"),
        app_commands.Choice(name="Enchanted (Emoji)", value="emoji")
    ])
    @app_commands.choices(display_format=[
        app_commands.Choice(name="Embed 🌸", value="embed"),
        app_commands.Choice(name="Plain Text ☁️", value="plain")
    ])
    async def decrypt(self, interaction: discord.Interaction, code: str, mode: str, display_format: str = "embed"):
        try:
            result = self.do_decrypt(code, mode)
            
            if display_format == "embed":
                embed = discord.Embed(
                    title="🔓 Decoded Message",
                    description=f"**Original Mode:** `{mode.upper()}`\n\n> {result}",
                    color=0xffb7c5
                )
                embed.set_footer(text="Shh... this message is only visible to you! 🕵️‍♂️")
                # Ephemeral ensures only the user running the command sees the output
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                # Plain text format, also kept ephemeral
                await interaction.response.send_message(f"🔓 **Decoded Message ({mode.upper()}):**\n> {result}", ephemeral=True)
                
        except Exception:
            await interaction.response.send_message("❌ **Error:** Invalid code for this mode! 🌸", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Encryption(bot))
