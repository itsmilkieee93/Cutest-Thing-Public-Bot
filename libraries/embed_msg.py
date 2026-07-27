# encoding: utf-8
import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import random
from datetime import datetime

# ─── Logger ───────────────────────────────────────────────────────────────────
os.makedirs('log', exist_ok=True)
logger = logging.getLogger('EmbedMsg')
logger.setLevel(logging.INFO)
handler = logging.FileHandler(
    '/sdcard/script/bot/log/embed_msg.log', encoding='utf-8'
)
handler.setFormatter(logging.Formatter(
    '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
if not logger.handlers:
    logger.addHandler(handler)

# ─── Preset Colors ────────────────────────────────────────────────────────────
PRESET_COLORS = {
    "pink":    0xFFC0CB,
    "lavender":0xB57EDC,
    "blush":   0xFFD1DC,
    "mint":    0xB5EAD7,
    "peach":   0xFFDAB9,
    "yellow":  0xFFF0A0,
    "lilac":   0xC9C0D3,
    "sky":     0xA8D8EA,
    "red":     0xFF6B6B,
    "blue":    0x5865F2,
    "green":   0x57F287,
    "orange":  0xFFB347,
    "white":   0xFFFFFF,
    "black":   0x23272A,
    "random":  None,  # handled separately
}


class EmbedMsgCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── Helper: resolve color ────────────────────────────────────────────────
    def _resolve_color(self, color_choice: str, hex_color: str | None) -> int:
        """Return final int color from preset or custom hex."""
        # 1. Custom hex takes priority if provided
        if hex_color:
            raw = hex_color.strip().lstrip("#").lstrip("0x").lstrip("0X")
            try:
                return int(raw, 16)
            except ValueError:
                pass  # fall through to preset

        # 2. Preset
        if color_choice == "random":
            return random.choice([
                0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xB5EAD7,
                0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xA8D8EA,
            ])
        return PRESET_COLORS.get(color_choice, 0xB57EDC)

    # ─── Helper: apply text style ────────────────────────────────────────────
    def _apply_style(self, text: str, style: str) -> str:
        """Wrap text in Discord markdown based on style."""
        if not text:
            return text
        match style:
            case "bold":        return f"**{text}**"
            case "italic":      return f"*{text}*"
            case "bold_italic": return f"***{text}***"
            case "underline":   return f"__{text}__"
            case "strikethrough": return f"~~{text}~~"
            case "code":        return f"`{text}`"
            case "codeblock":   return f"```\n{text}\n```"
            case "quote":       return f"> {text}"
            case _:             return text  # plain

    # ─── /embed-msg ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="embed-msg",
        description="Send a beautiful customizable embed message! 🎨✨"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        channel      = "Channel to send the embed to 📺 (defaults to current channel)",
        title        = "Title of the embed 📝",
        description  = "Main body text of the embed 💬",
        color        = "Pick a preset color 🎨",
        hex_color    = "Custom hex color (e.g. FF5733) — overrides color preset 🖌️",
        text_style   = "Apply a style to the description text ✍️",
        image_url    = "Full URL of an image to display inside the embed 🖼️",
        thumbnail_url= "Full URL of a small thumbnail (top-right corner) 🔲",
        footer       = "Footer text at the bottom of the embed 🗒️",
        author       = "Author name shown at the top of the embed 👤",
        timestamp    = "Show current timestamp on the embed? ⏰",
        ephemeral    = "Only you can see the preview confirmation? 👁️",
    )
    @app_commands.choices(color=[
        app_commands.Choice(name="🌸 Pink",      value="pink"),
        app_commands.Choice(name="💜 Lavender",  value="lavender"),
        app_commands.Choice(name="🌷 Blush",     value="blush"),
        app_commands.Choice(name="🍃 Mint",      value="mint"),
        app_commands.Choice(name="🍑 Peach",     value="peach"),
        app_commands.Choice(name="🌼 Yellow",    value="yellow"),
        app_commands.Choice(name="🫧 Lilac",     value="lilac"),
        app_commands.Choice(name="☁️ Sky Blue",  value="sky"),
        app_commands.Choice(name="❤️ Red",       value="red"),
        app_commands.Choice(name="💙 Blue",      value="blue"),
        app_commands.Choice(name="💚 Green",     value="green"),
        app_commands.Choice(name="🟠 Orange",    value="orange"),
        app_commands.Choice(name="⬜ White",     value="white"),
        app_commands.Choice(name="⬛ Black",     value="black"),
        app_commands.Choice(name="🎲 Random",    value="random"),
    ])
    @app_commands.choices(text_style=[
        app_commands.Choice(name="Plain (Default)",       value="plain"),
        app_commands.Choice(name="**Bold**",              value="bold"),
        app_commands.Choice(name="*Italic*",              value="italic"),
        app_commands.Choice(name="***Bold Italic***",     value="bold_italic"),
        app_commands.Choice(name="__Underline__",         value="underline"),
        app_commands.Choice(name="~~Strikethrough~~",     value="strikethrough"),
        app_commands.Choice(name="`Code`",                value="code"),
        app_commands.Choice(name="```Code Block```",      value="codeblock"),
        app_commands.Choice(name="> Quote",               value="quote"),
    ])
    @app_commands.choices(timestamp=[
        app_commands.Choice(name="✅ Yes — show timestamp", value="yes"),
        app_commands.Choice(name="❌ No  — hide timestamp", value="no"),
    ])
    @app_commands.choices(ephemeral=[
        app_commands.Choice(name="👁️ Yes — only I see the confirmation", value="yes"),
        app_commands.Choice(name="💬 No  — confirmation visible to all",  value="no"),
    ])
    async def embed_msg(
        self,
        interaction: discord.Interaction,
        description:   str,
        channel:       discord.TextChannel    = None,
        title:         str                    = "",
        color:         str                    = "lavender",
        hex_color:     str | None             = None,
        text_style:    str                    = "plain",
        image_url:     str | None             = None,
        thumbnail_url: str | None             = None,
        footer:        str | None             = None,
        author:        str | None             = None,
        timestamp:     str                    = "no",
        ephemeral:     str                    = "yes",
    ):
        await interaction.response.defer(ephemeral=(ephemeral == "yes"))

        # ── Validate image URLs ────────────────────────────────────────────────
        def is_valid_url(url: str | None) -> bool:
            if not url:
                return False
            return url.startswith("http://") or url.startswith("https://")

        if image_url and not is_valid_url(image_url):
            err = discord.Embed(
                title="❌ Invalid Image URL",
                description="Please provide a valid URL starting with `http://` or `https://` 🌐",
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err, ephemeral=True)

        if thumbnail_url and not is_valid_url(thumbnail_url):
            err = discord.Embed(
                title="❌ Invalid Thumbnail URL",
                description="Please provide a valid URL starting with `http://` or `https://` 🌐",
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err, ephemeral=True)

        # ── Build the embed ───────────────────────────────────────────────────
        final_color       = self._resolve_color(color, hex_color)
        styled_description = self._apply_style(description, text_style)

        embed = discord.Embed(
            title       = title or None,
            description = styled_description,
            color       = final_color,
            timestamp   = datetime.now() if timestamp == "yes" else None,
        )

        if author:
            embed.set_author(
                name    = author,
                icon_url= interaction.user.display_avatar.url
            )

        if image_url:
            embed.set_image(url=image_url)

        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        if footer:
            embed.set_footer(
                text     = footer,
                icon_url = interaction.user.display_avatar.url
            )

        # ── Resolve target channel ────────────────────────────────────────────
        target_channel = channel or interaction.channel

        # ── Send the embed to the channel ─────────────────────────────────────
        try:
            await target_channel.send(embed=embed)
        except discord.Forbidden:
            err = discord.Embed(
                title="❌ Missing Permissions",
                description=f"I don't have permission to send messages in {target_channel.mention}! 😭",
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err, ephemeral=True)
        except discord.HTTPException as e:
            # Image URL might be invalid/unreachable by Discord
            err = discord.Embed(
                title="❌ Failed to Send Embed",
                description=(
                    f"`{str(e)[:200]}`\n\n"
                    "This is usually caused by an invalid or unreachable image URL. 🖼️"
                ),
                color=0xFF6B6B
            )
            return await interaction.followup.send(embed=err, ephemeral=True)

        # ── Confirmation ──────────────────────────────────────────────────────
        confirm = discord.Embed(
            title="✅ Embed Sent!",
            description=f"Your beautiful embed has been delivered to {target_channel.mention}! 🌸✨",
            color=final_color
        )
        confirm.set_footer(text=f"Sent by {interaction.user.display_name} 😊")
        await interaction.followup.send(
            embed    = confirm,
            ephemeral= (ephemeral == "yes")
        )

        logger.info(
            f"📨 CMD [/embed-msg] | User: {interaction.user} ({interaction.user.id}) "
            f"| Channel: #{target_channel.name} ({target_channel.id}) "
            f"| Color: {color} | HexOverride: {hex_color} | Style: {text_style} "
            f"| Image: {bool(image_url)} | Thumbnail: {bool(thumbnail_url)}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedMsgCog(bot))
