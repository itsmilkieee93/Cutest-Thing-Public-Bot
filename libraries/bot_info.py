import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
import random
import platform 

# 🌸 NEW: Config loader for Discord snowflake IDs
from config_loader import get_owner_id, get_birthday_ts, get_command_id, get_all_commands

# ── Safe version import — works both as package and direct import ──────────────
try:
    from . import __version__, __bot_name__, __description__, __discord__
except ImportError:
    import importlib.util, os as _os
    _spec = importlib.util.spec_from_file_location(
        "libraries",
        _os.path.join(_os.path.dirname(__file__), "__init__.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    __version__     = _mod.__version__
    __bot_name__    = _mod.__bot_name__
    __description__ = _mod.__description__
    __discord__     = _mod.__discord__

class UtilityCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lavender = 0xB57EDC  # 🎀 Soft Lavender Aesthetic
        self.sparkles = ["😊", "✨", "💖", "🙌", "🌸", "🍀", "🌟", "🕊️", "🌿", "💫", "🌈", "💎", "🎀", "🥰", "💞", "🥹"]

        # 🌸 Load command IDs from config instead of hardcoding
        self.cmd_ids = get_all_commands()

    def _format_cmd(self, category: str, cmd_name: str) -> str:
        """🌸 Build a Discord slash-command link from the config ID."""
        cmd_id = get_command_id(category, cmd_name)
        slug = cmd_name.replace("_", "-")
        return f"</{slug}:{cmd_id}>" if cmd_id else f"`/{slug}`"

    async def bridge_log(self, interaction: discord.Interaction, command_name: str, status: str, result: str):
        """Helper to ensure everything is tracked in your logs! 🛰️"""
        try:
            from parameter import bridge_log as log_func
            await log_func(interaction, command_name, status, result)
        except ImportError:
            pass

    # ── 🌸 Per-server persona helpers ────────────────────────────────────
    # If this server has a custom nickname/avatar set (via /server-persona-set),
    # use it. Otherwise fall back to the bot's global name/avatar.
    def _display_name(self, interaction: discord.Interaction) -> str:
        if interaction.guild is not None and interaction.guild.me is not None:
            return interaction.guild.me.display_name
        return __bot_name__

    def _display_avatar_url(self, interaction: discord.Interaction) -> str:
        # 🌸 Member.display_avatar already prefers the per-guild avatar
        # over the global one when the bot has one set for this server.
        if interaction.guild is not None and interaction.guild.me is not None:
            return interaction.guild.me.display_avatar.url
        return self.bot.user.display_avatar.url

    @app_commands.command(name="info", description="Check my heartbeat and connection status! 📡")
    async def info(self, interaction: discord.Interaction):
        # 1. Tell Discord to wait for the mobile server ⏳
        await interaction.response.defer()
        
        latency = round(self.bot.latency * 1000)
        emoji = random.choice(self.sparkles)
        
        embed = discord.Embed(
            title=f"{emoji} Bridge Status",
            description="The system is running smoothly and ready for instructions! ✨",
            color=self.lavender,
            timestamp=datetime.now()
        )
        embed.add_field(name="📡 Latency", value=f"`{latency}ms`", inline=True)
        embed.add_field(name="🛰️ Connection", value="`Stable`", inline=True)
        embed.set_footer(text=self._display_name(interaction))
        
        # 2. Use followup to send the message 💌
        await interaction.followup.send(embed=embed)
        await self.bridge_log(interaction, "info", "Latency Check", f"{latency}ms")

    @app_commands.command(name="help", description="View my command manual and learn how to use me! 💖")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help(self, interaction: discord.Interaction):
        await interaction.response.defer()

        emoji = random.choice(self.sparkles)

        embed = discord.Embed(
            title=f"✨ Command Manual {emoji}",
            description=(
                f"Hello there! Welcome to **{self._display_name(interaction)}** — here's everything I can do for you!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=self.lavender,
            timestamp=datetime.now()
        )

        # ── 🤖 AI & Intelligence ──────────────────────────────────────────────
        gemini_cmd, gemini_reply_cmd = self._format_cmd("ai", "gemini"), self._format_cmd("ai", "gemini_reply")
        openrouter_cmd, openrouter_reply_cmd = self._format_cmd("ai", "openrouter"), self._format_cmd("ai", "openrouter_reply")
        cloudflare_ai_cmd, cloudflare_ai_reply_cmd = self._format_cmd("ai", "cloudflare_ai"), self._format_cmd("ai", "cloudflare_ai_reply")

        embed.add_field(
            name="🤖 ᴀ ɪ  &  ɪ ɴ ᴛ ᴇ ʟ ʟ ɪ ɢ ᴇ ɴ ᴄ ᴇ",
            value=(
                f"{gemini_cmd}  {gemini_reply_cmd}\n"
                "Chat with Gemini AI — supports memory across messages.\n"
                f"> 📌 {gemini_cmd} `prompt:What is life?` | {gemini_reply_cmd} `message_id:... prompt:...`\n\n"

                f"{openrouter_cmd}  {openrouter_reply_cmd}\n"
                "Chat using OpenRouter's AI models.\n"
                f"> 📌 {openrouter_cmd} `prompt:Hello!` | {openrouter_reply_cmd} `message_id:... prompt:...`\n\n"

                f"{cloudflare_ai_cmd}  {cloudflare_ai_reply_cmd}\n"
                "Chat using Cloudflare AI models. ☁️\n"
                f"> 📌 {cloudflare_ai_cmd} `prompt:Hello!` | {cloudflare_ai_reply_cmd} `message_id:... prompt:...`"
            ),
            inline=False
        )

        # ── 📡 Communication ──────────────────────────────────────────────────
        msg_cmd = self._format_cmd("messaging", "msg")
        edit_msg_cmd = self._format_cmd("messaging", "edit_msg")
        dm_user_cmd = self._format_cmd("messaging", "dm_user")
        forward_msg_cmd = self._format_cmd("messaging", "forward_msg")
        embed_msg_cmd = self._format_cmd("messaging", "embed_msg")

        embed.add_field(
            name="📡 ꜱ ᴇ ɴ ᴅ  &  ᴄ ᴏ ᴍ ᴍ ᴜ ɴ ɪ ᴄ ᴀ ᴛ ɪ ᴏ ɴ",
            value=(
                f"{msg_cmd}\n"
                "Send or reply to a message in any channel.\n"
                f"> 📌 {msg_cmd} `channel:#general text:Hello everyone!`\n\n"

                f"{edit_msg_cmd}\n"
                "Edit a message the bot previously sent.\n"
                f"> 📌 {edit_msg_cmd} `message_id:... new_text:Updated content`\n\n"

                f"{dm_user_cmd}\n"
                "Send a private message to anyone via their User ID.\n"
                f"> 📌 {dm_user_cmd} `user_id:123456789 text:Hey!`\n\n"

                f"{forward_msg_cmd}\n"
                "Relay a message across servers or channels.\n"
                f"> 📌 {forward_msg_cmd} `message_id:... to:#channel`\n\n"

                f"{embed_msg_cmd}\n"
                "Send a beautiful customizable embed message.\n"
                f"> 📌 {embed_msg_cmd} `description:Hello! color:pink image_url:...`"
            ),
            inline=False
        )

        # ── 🎭 Interaction & Moderation ───────────────────────────────────────
        react_cmd = self._format_cmd("interaction", "react")
        unreact_cmd = self._format_cmd("interaction", "unreact")
        clear_msg_cmd = self._format_cmd("interaction", "clear_msg")
        safs_cmd = self._format_cmd("interaction", "safs")
        unf_cmd = self._format_cmd("interaction", "unf")

        embed.add_field(
            name="🎭 ɪ ɴ ᴛ ᴇ ʀ ᴀ ᴄ ᴛ ɪ ᴏ ɴ  &  ᴍ ᴏ ᴅ ᴇ ʀ ᴀ ᴛ ɪ ᴏ ɴ",
            value=(
                f"{react_cmd}  {unreact_cmd}\n"
                "Add or remove a reaction emoji on any message by ID.\n"
                f"> 📌 {react_cmd} `message_id:... emoji:🌸` | {unreact_cmd} `message_id:... emoji:🌸`\n\n"

                f"{clear_msg_cmd}\n"
                "Clean up recent bot messages from the channel.\n"
                f"> 📌 {clear_msg_cmd} `amount:10`\n\n"

                f"{safs_cmd}  {unf_cmd}\n"
                "Toggle safe or unfiltered content mode.\n"
                f"> 📌 {safs_cmd} | {unf_cmd}"
            ),
            inline=False
        )

        # ── 🔮 System & Tools ────────────────────────────────────────────────
        info_cmd = self._format_cmd("system", "info")
        speed_test_cmd = self._format_cmd("system", "speed_test")
        encrypt_cmd = self._format_cmd("system", "encrypt")
        decrypt_cmd = self._format_cmd("system", "decrypt")
        upload_cmd = self._format_cmd("system", "upload")

        embed.add_field(
            name="🔮 ꜱ ʏ ꜱ ᴛ ᴇ ᴍ  &  ᴛ ᴏ ᴏ ʟ ꜱ",
            value=(
                f"{info_cmd}\n"
                "View connection status and latency stats. 📡\n"
                f"> 📌 {info_cmd}\n\n"

                f"{speed_test_cmd}\n"
                "Run a live speed test on my owner's WiFi. 🛜\n"
                f"> 📌 {speed_test_cmd}\n\n"

                f"{encrypt_cmd}  {decrypt_cmd}\n"
                "Convert text ↔ Base64 encoded strings.\n"
                f"> 📌 {encrypt_cmd} `text:Hello World` → `SGVsbG8gV29ybGQ=`\n"
                f"> 📌 {decrypt_cmd} `text:SGVsbG8gV29ybGQ=` → `Hello World`\n\n"

                f"{upload_cmd}\n"
                "Upload a file through the bot.\n"
                f"> 📌 {upload_cmd} `file:...`"
            ),
            inline=False
        )

        # ── 🧮 Math ───────────────────────────────────────────────────────────
        math_cmd = self._format_cmd("math", "math")
        math_ref_cmd = self._format_cmd("math", "math_ref")

        embed.add_field(
            name="🧮 ᴍ ᴀ ᴛ ʜ  ꜱ ᴏ ʟ ᴠ ᴇ ʀ",
            value=(
                f"{math_cmd}\n"
                "Solve any math expression or arithmetic operation.\n"
                f"> 📌 {math_cmd} `expression:sqrt(144) + factorial(5)`\n"
                f"> 📌 {math_cmd} `number_a:25 operator:➗ number_b:5`\n\n"

                f"{math_ref_cmd}\n"
                "View all available math functions and constants.\n"
                f"> 📌 {math_ref_cmd}"
            ),
            inline=False
        )

        # ── 📚 Knowledge & Joy ────────────────────────────────────────────────
        fact_cmd = self._format_cmd("knowledge", "fact")
        quote_cmd = self._format_cmd("knowledge", "quote")
        advice_cmd = self._format_cmd("knowledge", "advice")
        dadjoke_cmd = self._format_cmd("knowledge", "dadjoke")
        question_cmd = self._format_cmd("knowledge", "question")
        wikipedia_cmd = self._format_cmd("knowledge", "wikipedia")
        wiki_news_cmd = self._format_cmd("knowledge", "wiki_news")
        news_cmd = self._format_cmd("knowledge", "news")
        weather_cmd = self._format_cmd("knowledge", "weather")
        praise_cmd = self._format_cmd("knowledge", "praise")
        joke_unf_cmd = self._format_cmd("knowledge", "joke_unf")

        embed.add_field(
            name="📚 ᴋ ɴ ᴏ ᴡ ʟ ᴇ ᴅ ɢ ᴇ  &  ᴊ ᴏ ʏ",
            value=(
                "Random fact, quote, life advice, or dad joke.\n"
                f"> 📌 {fact_cmd} | {quote_cmd} | {advice_cmd} | {dadjoke_cmd}\n\n"

                f"{question_cmd}\n"
                "Get a random thought-provoking question. 🤔\n"
                f"> 📌 {question_cmd}\n\n"

                f"{wikipedia_cmd}  {wiki_news_cmd}\n"
                "Search Wikipedia or fetch latest Wikipedia news.\n"
                f"> 📌 {wikipedia_cmd} `query:Black holes` | {wiki_news_cmd} `topic:Technology`\n\n"

                f"{news_cmd}\n"
                "Fetch recent global news headlines.\n"
                f"> 📌 {news_cmd}\n\n"

                f"{weather_cmd}\n"
                "Get detailed weather info for any city. *(1000 req/day)*\n"
                f"> 📌 {weather_cmd} `city:Tokyo`\n\n"

                f"{praise_cmd}  {joke_unf_cmd}\n"
                "Send appreciation to a user 🥰 or get unfiltered humor ⚠️\n"
                f"> 📌 {praise_cmd} `user:@someone` | {joke_unf_cmd}"
            ),
            inline=False
        )

        # ── 🎵 Music & Media ──────────────────────────────────────────────────
        music_cmd = self._format_cmd("media", "music")
        download_music_cmd = self._format_cmd("media", "download_music")
        youtube_cmd = self._format_cmd("media", "youtube")
        livestream_chat_cmd = self._format_cmd("media", "livestream_chat")
        my_channel_cmd = self._format_cmd("media", "my_channel")

        embed.add_field(
            name="🎵 ᴍ ᴜ ꜱ ɪ ᴄ  &  ᴍ ᴇ ᴅ ɪ ᴀ",
            value=(
                f"{music_cmd}\n"
                "Search and listen to music via YouTube or YouTube Music. 🎶\n"
                f"> 📌 {music_cmd} `search:Blinding Lights`\n\n"

                f"{download_music_cmd}\n"
                "Download a song and save it to your device. 📥\n"
                f"> 📌 {download_music_cmd} `search:Starboy format:MP3 quality:192kbps`\n\n"

                f"{youtube_cmd}\n"
                "Get full stats and metadata from any YouTube video or playlist. 📊\n"
                f"> 📌 {youtube_cmd} `url:https://youtube.com/watch?v=...`\n\n"

                f"{livestream_chat_cmd}\n"
                "Interact with a YouTube livestream chat in real time. 📺\n"
                f"> 📌 {livestream_chat_cmd} `url:https://youtube.com/watch?v=...`\n\n"

                f"{my_channel_cmd}\n"
                "View stats and info about your YouTube channel. 🌟\n"
                f"> 📌 {my_channel_cmd}"
            ),
            inline=False
        )

        # ── 📸 Photo & Images ─────────────────────────────────────────────────
        photo_cmd = self._format_cmd("images", "photo")
        pexels_cmd = self._format_cmd("images", "pexels")

        embed.add_field(
            name="📸 ᴘ ʜ ᴏ ᴛ ᴏ  &  ɪ ᴍ ᴀ ɢ ᴇ ꜱ",
            value=(
                f"{photo_cmd}\n"
                "Search and display beautiful photos. 🖼️\n"
                f"> 📌 {photo_cmd} `query:Sunset mountains`\n\n"

                f"{pexels_cmd}\n"
                "Search high-quality stock photos from Pexels. 🌄\n"
                f"> 📌 {pexels_cmd} `query:Ocean waves`"
            ),
            inline=False
        )

        about_cmd = self._format_cmd("system", "about")
        embed.set_footer(
            text=f"v{__version__} 🌸  •  Use {about_cmd} to learn more about me!",
            icon_url=interaction.user.display_avatar.url
        )

        await interaction.followup.send(embed=embed)
        await self.bridge_log(interaction, "help", "Self", "Delivered Polished Help UI ✨️")

    @app_commands.command(name="about", description="Learn more about me and my creator! 🥰")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def about(self, interaction: discord.Interaction):
        # 1. Tell Discord to wait ⏳
        await interaction.response.defer()
        
        emoji = random.choice(self.sparkles)
        
        # 🌸 REFACTORED: both now live in discord_config.json
        # Exact Timestamp: April 14, 2026, 23:55:30 UTC
        birthday_ts = get_birthday_ts()
        owner_id = get_owner_id()

        # 🌸 Dynamic version fetching logic
        py_version = platform.python_version()
        dpy_version = discord.__version__

        # 2. Build the Lavender Embed
        embed = discord.Embed(
            title=self._display_name(interaction),
            description=f"Hello there! Awesome People! {emoji}",
            color=self.lavender
        )
        
        # User Mention for the Owner
        embed.add_field(name="👑 **Owner & Creator**", value=f"<@{owner_id}>", inline=False)
        
        # Birthday with Exact Clock & Relative Time
        embed.add_field(
            name="🎂 **Creation Date**", 
            value=f"<t:{birthday_ts}:F>\n↳ *Created <t:{birthday_ts}:R>*", 
            inline=False
        )
        
        # Tech Specs
        embed.add_field(name="⚙️ **Discord API Version **", value=f"`v{__discord__}`", inline=True)
        embed.add_field(name="🤖 **Bot Version **", value=f"`v{__version__}`", inline=True)
        # Displays the real version from Termux! 🐍
        embed.add_field(name="💻 **Python Version**", value=f"`v{py_version}`", inline=True)
        # discord.py library version 💖
        embed.add_field(name="📦 **discord.py Version**", value=f"`v{dpy_version}`", inline=True)
        
        embed.set_footer(text="Have a great day! 😊")
        
        # Add Thumbnail if available
        embed.set_thumbnail(url=self._display_avatar_url(interaction))
            
        # 3. Use followup to send the message 💌
        await interaction.followup.send(embed=embed)
        
        # Log the Interaction
        await self.bridge_log(interaction, "about", "Self", "Displayed About UI")


async def setup(bot):
    await bot.add_cog(UtilityCommands(bot))
