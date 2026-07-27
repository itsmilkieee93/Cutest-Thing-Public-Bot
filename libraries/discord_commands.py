"""
🌸 Discord Commands & Cogs Registration Module
Centralized registration of all bot cogs to keep bot_service.py clean.
"""

import sys
import os

# 1. Temukan jalur fisik asli dari file ini (aman dari symlink)
current_dir = os.path.dirname(os.path.realpath(__file__))

# 2. Gunakan os.path.join untuk naik satu tingkat ke folder utama 'bot'
bot_root_dir = os.path.realpath(os.path.join(current_dir, ".."))

# 3. Masukkan folder utama 'bot' ke sys.path agar folder 'libraries' bisa terbaca
if bot_root_dir not in sys.path:
    sys.path.insert(0, bot_root_dir)

# Struktur asli Anda tetap terjaga di bawah ini
from libraries import *
import system_commands


async def register_all_cogs(bot):
    """
    🌸 Register all bot cogs.
    
    Call this from bot.setup_hook() to load all command modules and cogs.
    This keeps the bot_service.py file clean and organized.
    
    Args:
        bot: The EnchantedBot instance
    """
    
    total_commands = 0
    
    # Register all split command modules (command groups)
    print("📦 Registering Command Modules:")
    modules = [
        ("commands_ai", commands_ai),
        ("commands_fun", commands_fun),
        ("commands_messaging", commands_messaging),
        ("commands_utility", commands_utility),
    ]
    
    for module_name, module in modules:
        try:
            module.setup_commands(bot)
            print(f"  ✅ {module_name}")
        except Exception as e:
            print(f"  ⚠️ {module_name}: {e}")
    
    # Register individual cogs
    print("\n🎯 Registering Cogs:")
    cogs = [
        wikipedia.CutestThing(bot),
        bot_info.UtilityCommands(bot),
        news.NewsModule(bot),
        news_api.CurrentsNewsModule(bot),
        music.MusicModule(bot),
        forward.ForwardCog(bot),
        weather.WeatherModule(bot),
        yt.YouTubeUniversalModule(bot),
        wifi.SpeedTestModule(bot),
        enc.Encryption(bot),
        downloader.MusicDownloader(bot),
        embed.EmbedMsgCog(bot),
        calculator.MathSolverCog(bot),
        chatting_fun.LiveStreamChatModule(bot),
        channel.YouTubeAnalyticsCog(bot),
        unsplash.UnsplashCog(bot),
        pexels.PexelsCog(bot),
        openrouter.OpenRouterCog(bot),
        cake.CloudflareAICog(bot),
        brief.SummarizeCog(bot),
        perms.PermissionsCog(bot),
        random_msg.RandomMsgCog(bot),
        server_info.ServerInfoCog(bot),
        cute.ServerPersona(bot),
        gen.PhotoEditorCog(bot),
        system_commands.SystemCommands(bot),
        status_bot.StatusCommands(bot)
    ]
    
    for cog in cogs:
        try:
            await bot.add_cog(cog)
            cog_name = cog.__class__.__name__
            # Count commands in this cog
            cog_commands = len([cmd for cmd in bot.tree.get_commands() if hasattr(cmd, 'cog_name')])
            print(f"  ✅ {cog_name}")
        except Exception as e:
            print(f"  ⚠️ {cog.__class__.__name__}: {e}")
    
    total_cogs = len(cogs)
    total_modules = 4
    print(f"\n✅ Registered {total_cogs} cogs + {total_modules} command modules")
    print(f"📋 Total commands pending sync: {len(bot.tree.get_commands())}")
