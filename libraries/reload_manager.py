"""
🌸 Reload Manager — handles dynamic reloading of bot modules & cogs.

Kept separate from bot_service.py so the reload logic (which module list,
what order, cog re-registration) can be edited without touching the main
bot file. Import and call `reload_all(bot)` from bot_service.py's !reload
handler.
"""

import sys
import importlib


# 🌸 Modules reloaded on !reload, in dependency order.
# Config files go first since everything else may read from them.
MODULES_TO_RELOAD = [
    "discord_config", "config_loader",
    "groq_ai", "groq_instruct", "roulette",
    "wikipedia", "bot_info", "news", "news_api", "music",
    "forward_msg", "weather", "youtube", "wifi", "encryption",
    "music_downloader", "embed_msg", "calculator", "chatting_fun",
    "my_youtube_channel", "unsplash", "pexels", "openrouter",
    "cloudflare_ai", "commands_ai", "commands_fun",
    "commands_messaging", "commands_utility", "summarize",
    "permissions", "random_msg", "server_info",
    "discord_commands", "system_commands",
]


def reload_modules(module_names: list[str] = None) -> tuple[list[str], list[str]]:
    """
    🌸 Reimport each module already in sys.modules.
    Returns (succeeded, failed) lists of module names.
    """
    names = module_names or MODULES_TO_RELOAD
    succeeded, failed = [], []

    for module_name in names:
        try:
            module = sys.modules.get(module_name)
            if module:
                importlib.reload(module)
                succeeded.append(module_name)
                print(f"✅ Reloaded: {module_name}")
        except Exception as e:
            failed.append(module_name)
            print(f"⚠️ Failed to reload {module_name}: {e}")

    return succeeded, failed


async def reregister_cogs(bot) -> bool:
    """
    🌸 Remove all current cogs and re-register them fresh.
    Needed because importlib.reload() alone doesn't rebuild existing cog
    instances (e.g. bot_info.UtilityCommands would keep its stale
    self.cmd_ids cache without this).
    """
    try:
        for cog_name in list(bot.cogs.keys()):
            await bot.remove_cog(cog_name)

        from discord_commands import register_all_cogs
        await register_all_cogs(bot)
        print("✅ Cogs re-registered with fresh config!")
        return True
    except Exception as e:
        print(f"⚠️ Failed to re-register cogs: {e}")
        return False


def refresh_owner_id(bot) -> bool:
    """🌸 owner_id is cached on the bot instance at __init__ — refresh it
    in case it changed in discord_config.py."""
    try:
        from config_loader import get_owner_id
        bot.owner_id = get_owner_id()
        print(f"✅ owner_id refreshed: {bot.owner_id}")
        return True
    except Exception as e:
        print(f"⚠️ Failed to refresh owner_id: {e}")
        return False


async def reload_all(bot, module_names: list[str] = None) -> dict:
    """
    🌸 Full reload pipeline: reimport modules, re-register cogs, refresh
    cached config values on the bot instance. Call this from !reload.

    Returns a summary dict for logging/status messages.
    """
    succeeded, failed = reload_modules(module_names)
    cogs_ok = await reregister_cogs(bot)
    owner_ok = refresh_owner_id(bot)

    return {
        "modules_reloaded": succeeded,
        "modules_failed": failed,
        "cogs_reregistered": cogs_ok,
        "owner_id_refreshed": owner_ok,
    }
