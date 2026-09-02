import discord
from discord import ui, app_commands
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
        now_ts = int(datetime.now().timestamp())

        # 🌸 Components V2 — Container replaces the Embed
        container = ui.Container(
            ui.TextDisplay(
                f"### {emoji} Bridge Status\n"
                "The system is running smoothly and ready for instructions! ✨"
            ),
            ui.Separator(),
            ui.TextDisplay(
                f"**📡 Latency**\n`{latency}ms`\n\n"
                "**🛰️ Connection**\n`Stable`"
            ),
            ui.Separator(),
            ui.TextDisplay(f"-# {self._display_name(interaction)} • <t:{now_ts}:R>"),
            accent_color=self.lavender,
        )
        layout = ui.LayoutView()
        layout.add_item(container)

        # 2. Use followup to send the message 💌
        await interaction.followup.send(view=layout)
        await self.bridge_log(interaction, "info", "Latency Check", f"{latency}ms")

    @app_commands.command(name="help", description="View my command manual and learn how to use me! 💖")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help(self, interaction: discord.Interaction):
        await interaction.response.defer()

        emoji = random.choice(self.sparkles)
        container = ui.Container(accent_color=self.lavender)

        container.add_item(ui.TextDisplay(
            f"### ✨ Command Manual {emoji}\n"
            f"Hello there! Welcome to **{self._display_name(interaction)}** — here's everything I can do for you!"
        ))
        container.add_item(ui.Separator())

        # ── 🤖 AI & Intelligence ──────────────────────────────────────────────
        gemini_cmd, gemini_reply_cmd = self._format_cmd("ai", "gemini"), self._format_cmd("ai", "gemini_reply")
        openrouter_cmd, openrouter_reply_cmd = self._format_cmd("ai", "openrouter"), self._format_cmd("ai", "openrouter_reply")
        cloudflare_ai_cmd, cloudflare_ai_reply_cmd = self._format_cmd("ai", "cloudflare_ai"), self._format_cmd("ai", "cloudflare_ai_reply")

        container.add_item(ui.TextDisplay(
            "**🤖 ᴀ ɪ  &  ɪ ɴ ᴛ ᴇ ʟ ʟ ɪ ɢ ᴇ ɴ ᴄ ᴇ**\n"
            f"{gemini_cmd}\n"
            f"{gemini_reply_cmd}\n"
            "Chat with Gemini AI — supports memory across messages.\n"
            f"> 📌 `prompt:What is life?`\n\n"

            f"{openrouter_cmd}\n"
            f"{openrouter_reply_cmd}\n"
            "Chat using OpenRouter's AI models.\n"
            f"> 📌 `prompt:Hello!`\n\n"

            f"{cloudflare_ai_cmd}\n"
            f"{cloudflare_ai_reply_cmd}\n"
            "Chat using Cloudflare AI models. ☁️\n"
            f"> 📌 `prompt:Hello!`"
        ))
        container.add_item(ui.Separator())

        # ── 📡 Communication ──────────────────────────────────────────────────
        msg_cmd = self._format_cmd("messaging", "msg")
        edit_msg_cmd = self._format_cmd("messaging", "edit_msg")
        dm_user_cmd = self._format_cmd("messaging", "dm_user")
        forward_msg_cmd = self._format_cmd("messaging", "forward_msg")
        embed_msg_cmd = self._format_cmd("messaging", "embed_msg")

        container.add_item(ui.TextDisplay(
            "**📡 ꜱ ᴇ ɴ ᴅ  &  ᴄ ᴏ ᴍ ᴍ ᴜ ɴ ɪ ᴄ ᴀ ᴛ ɪ ᴏ ɴ**\n"
            f"{msg_cmd}\n"
            "Send or reply to a message in any channel.\n"
            f"> 📌 `channel:#general text:Hello everyone!`\n\n"

            f"{edit_msg_cmd}\n"
            "Edit a message the bot previously sent.\n"
            f"> 📌 `message_id:... new_text:Updated content`\n\n"

            f"{dm_user_cmd}\n"
            "Send a private message to anyone via their User ID.\n"
            f"> 📌 `user_id:123456789 text:Hey!`\n\n"

            f"{forward_msg_cmd}\n"
            "Relay a message across servers or channels.\n"
            f"> 📌 `message_id:... to:#channel`\n\n"

            f"{embed_msg_cmd}\n"
            "Send a beautiful customizable embed message.\n"
            f"> 📌 `description:Hello! color:pink image_url:...`"
        ))
        container.add_item(ui.Separator())

        # ── 🎭 Interaction & Moderation ───────────────────────────────────────
        react_cmd = self._format_cmd("interaction", "react")
        unreact_cmd = self._format_cmd("interaction", "unreact")
        clear_msg_cmd = self._format_cmd("interaction", "clear_msg")
        safs_cmd = self._format_cmd("interaction", "safs")
        unf_cmd = self._format_cmd("interaction", "unf")

        container.add_item(ui.TextDisplay(
            "**🎭 ɪ ɴ ᴛ ᴇ ʀ ᴀ ᴄ ᴛ ɪ ᴏ ɴ  &  ᴍ ᴏ ᴅ ᴇ ʀ ᴀ ᴛ ɪ ᴏ ɴ**\n"
            f"{react_cmd}\n"
            f"{unreact_cmd}\n"
            "Add or remove a reaction emoji on any message by ID.\n"
            f"> 📌 `message_id:... emoji:🌸`\n\n"

            f"{clear_msg_cmd}\n"
            "Clean up recent bot messages from the channel.\n"
            f"> 📌 `amount:10`\n\n"

            f"{safs_cmd}\n"
            f"{unf_cmd}\n"
            "Toggle safe or unfiltered content mode."
        ))
        container.add_item(ui.Separator())

        # ── 🔮 System & Tools ────────────────────────────────────────────────
        info_cmd = self._format_cmd("system", "info")
        speed_test_cmd = self._format_cmd("system", "speed_test")
        encrypt_cmd = self._format_cmd("system", "encrypt")
        decrypt_cmd = self._format_cmd("system", "decrypt")
        upload_cmd = self._format_cmd("system", "upload")

        container.add_item(ui.TextDisplay(
            "**🔮 ꜱ ʏ ꜱ ᴛ ᴇ ᴍ  &  ᴛ ᴏ ᴏ ʟ ꜱ**\n"
            f"{info_cmd}\n"
            "View connection status and latency stats. 📡\n\n"

            f"{speed_test_cmd}\n"
            "Run a live speed test on my owner's WiFi. 🛜\n\n"

            f"{encrypt_cmd}\n"
            f"{decrypt_cmd}\n"
            "Convert text ↔ Base64 encoded strings.\n"
            f"> 📌 `text:Hello World` → `SGVsbG8gV29ybGQ=`\n\n"

            f"{upload_cmd}\n"
            "Upload a file through the bot.\n"
            f"> 📌 `file:...`"
        ))
        container.add_item(ui.Separator())

        # ── 🧮 Math ───────────────────────────────────────────────────────────
        math_cmd = self._format_cmd("math", "math")
        math_ref_cmd = self._format_cmd("math", "math_ref")

        container.add_item(ui.TextDisplay(
            "**🧮 ᴍ ᴀ ᴛ ʜ  ꜱ ᴏ ʟ ᴠ ᴇ ʀ**\n"
            f"{math_cmd}\n"
            "Solve any math expression or arithmetic operation.\n"
            f"> 📌 `expression:sqrt(144) + factorial(5)`\n"
            f"> 📌 `number_a:25 operator:➗ number_b:5`\n\n"

            f"{math_ref_cmd}\n"
            "View all available math functions and constants."
        ))
        container.add_item(ui.Separator())

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

        container.add_item(ui.TextDisplay(
            "**📚 ᴋ ɴ ᴏ ᴡ ʟ ᴇ ᴅ ɢ ᴇ  &  ᴊ ᴏ ʏ**\n"
            f"{fact_cmd}\n"
            f"{quote_cmd}\n"
            f"{advice_cmd}\n"
            f"{dadjoke_cmd}\n"
            "Random fact, quote, life advice, or dad joke.\n\n"

            f"{question_cmd}\n"
            "Get a random thought-provoking question. 🤔\n\n"

            f"{wikipedia_cmd}\n"
            f"{wiki_news_cmd}\n"
            "Search Wikipedia or fetch latest Wikipedia news.\n"
            f"> 📌 `query:Black holes` | `topic:Technology`\n\n"

            f"{news_cmd}\n"
            "Fetch recent global news headlines.\n\n"

            f"{weather_cmd}\n"
            "Get detailed weather info for any city. *(1000 req/day)*\n"
            f"> 📌 `city:Tokyo`\n\n"

            f"{praise_cmd}\n"
            f"{joke_unf_cmd}\n"
            "Send appreciation to a user 🥰 or get unfiltered humor ⚠️\n"
            f"> 📌 `user:@someone`"
        ))
        container.add_item(ui.Separator())

        # ── 🎵 Music & Media ──────────────────────────────────────────────────
        music_cmd = self._format_cmd("media", "music")
        download_music_cmd = self._format_cmd("media", "download_music")
        youtube_cmd = self._format_cmd("media", "youtube")
        livestream_chat_cmd = self._format_cmd("media", "livestream_chat")
        my_channel_cmd = self._format_cmd("media", "my_channel")

        container.add_item(ui.TextDisplay(
            "**🎵 ᴍ ᴜ ꜱ ɪ ᴄ  &  ᴍ ᴇ ᴅ ɪ ᴀ**\n"
            f"{music_cmd}\n"
            "Search and listen to music via YouTube or YouTube Music. 🎶\n"
            f"> 📌 `search:Blinding Lights`\n\n"

            f"{download_music_cmd}\n"
            "Download a song and save it to your device. 📥\n"
            f"> 📌 `search:Starboy format:MP3 quality:192kbps`\n\n"

            f"{youtube_cmd}\n"
            "Get full stats and metadata from any YouTube video or playlist. 📊\n"
            f"> 📌 `url:https://youtube.com/watch?v=...`\n\n"

            f"{livestream_chat_cmd}\n"
            "Interact with a YouTube livestream chat in real time. 📺\n"
            f"> 📌 `url:https://youtube.com/watch?v=...`\n\n"

            f"{my_channel_cmd}\n"
            "View stats and info about your YouTube channel. 🌟"
        ))
        container.add_item(ui.Separator())

        # ── 📸 Photo & Images ─────────────────────────────────────────────────
        photo_cmd = self._format_cmd("images", "photo")
        pexels_cmd = self._format_cmd("images", "pexels")

        container.add_item(ui.TextDisplay(
            "**📸 ᴘ ʜ ᴏ ᴛ ᴏ  &  ɪ ᴍ ᴀ ɢ ᴇ ꜱ**\n"
            f"{photo_cmd}\n"
            "Search and display beautiful photos. 🖼️\n"
            f"> 📌 `query:Sunset mountains`\n\n"

            f"{pexels_cmd}\n"
            "Search high-quality stock photos from Pexels. 🌄\n"
            f"> 📌 `query:Ocean waves`"
        ))
        container.add_item(ui.Separator())

        about_cmd = self._format_cmd("system", "about")
        container.add_item(ui.TextDisplay(
            f"-# v{__version__} 🌸  •  Use {about_cmd} to learn more about me!"
        ))

        layout = ui.LayoutView()
        layout.add_item(container)

        await interaction.followup.send(view=layout)
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

        # 🌸 Fetch the bot's banner — prefer the per-server override set via
        # /server-persona-set, fall back to the bot's global profile banner.
        # Neither guild.me (cached Member) nor bot.user (cached ClientUser)
        # carry banner data, so both need a fresh fetch to actually see it.
        banner_url = None
        try:
            if interaction.guild is not None:
                fresh_member = await interaction.guild.fetch_member(self.bot.user.id)
                if fresh_member.banner:
                    banner_url = fresh_member.banner.with_size(2048).url
            if banner_url is None:
                fresh_user = await self.bot.fetch_user(self.bot.user.id)
                if fresh_user.banner:
                    banner_url = fresh_user.banner.with_size(2048).url
        except Exception as e:
            print(f"⚠️ Failed to fetch bot banner: {e}")

        # 2. Build the Lavender Container (Components V2)
        header = ui.Section(
            ui.TextDisplay(
                f"### {self._display_name(interaction)}\n"
                f"Hello there! Awesome People! {emoji}"
            ),
            accessory=ui.Thumbnail(media=self._display_avatar_url(interaction)),
        )

        container = ui.Container(accent_color=self.lavender)
        container.add_item(header)
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            f"**👑 Owner & Creator**\n<@{owner_id}>"
        ))
        container.add_item(ui.TextDisplay(
            f"**🎂 Creation Date**\n<t:{birthday_ts}:F>\n↳ *Created <t:{birthday_ts}:R>*"
        ))
        container.add_item(ui.Separator())

        container.add_item(ui.TextDisplay(
            f"**⚙️ Discord API Version**\n`v{__discord__}`\n\n"
            f"**🤖 Bot Version**\n`v{__version__}`\n\n"
            f"**💻 Python Version**\n`v{py_version}`\n\n"
            f"**📦 discord.py Version**\n`v{dpy_version}`"
        ))

        # 🌸 High-res banner across the bottom (only if one is set)
        if banner_url:
            container.add_item(ui.Separator())
            container.add_item(ui.MediaGallery(discord.MediaGalleryItem(media=banner_url)))

        container.add_item(ui.Separator())
        container.add_item(ui.TextDisplay("-# Have a great day! 😊"))

        layout = ui.LayoutView()
        layout.add_item(container)

        # 3. Use followup to send the message 💌
        await interaction.followup.send(view=layout)

        # Log the Interaction
        await self.bridge_log(interaction, "about", "Self", "Displayed About UI")


async def setup(bot):
    await bot.add_cog(UtilityCommands(bot))
