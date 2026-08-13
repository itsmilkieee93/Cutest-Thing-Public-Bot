"""
🌸 Configuration Loader — reads Discord IDs from discord_config.py
instead of hardcoding snowflake IDs throughout the codebase.
"""

def _load():
    """Import discord_config.py, with a friendly error if it's missing."""
    try:
        import discord_config
        return discord_config
    except ImportError:
        print("⚠️ discord_config.py not found — copy discord_config.example.py "
              "to discord_config.py and fill in your real IDs.")
        return None


def get_owner_id() -> int:
    """Get bot owner ID from config."""
    cfg = _load()
    return cfg.BOT.get("owner_id") if cfg else None


def get_owner_ids() -> list[int]:
    """
    Get every owner ID from config — combines owner_ids with owner_id so
    older configs that only set owner_id (no owner_ids list) still work.
    """
    cfg = _load()
    if not cfg:
        return []
    ids = set(cfg.BOT.get("owner_ids", []))
    single = cfg.BOT.get("owner_id")
    if single:
        ids.add(single)
    return list(ids)


def is_owner(user_id: int) -> bool:
    """True if user_id is any configured owner."""
    return user_id in get_owner_ids()


def get_blocked_user_ids() -> list[int]:
    """Get list of blocked user IDs from config."""
    cfg = _load()
    return cfg.BOT.get("blocked_user_ids", []) if cfg else []


def is_blocked(user_id: int) -> bool:
    """True if user_id is on the blocklist — banned from using the bot."""
    return user_id in get_blocked_user_ids()


def get_test_guild_id() -> int:
    """Get test guild ID from config."""
    cfg = _load()
    return cfg.BOT.get("test_guild_id") if cfg else None


def get_allowed_server_ids() -> list[int]:
    """Get list of allowed server IDs from config."""
    cfg = _load()
    return cfg.BOT.get("allowed_server_ids", []) if cfg else []


def get_birthday_ts() -> int:
    """Get bot's birthday Unix timestamp from config."""
    cfg = _load()
    return cfg.BOT.get("birthday_ts", 0) if cfg else 0


def get_log_channel_ids() -> list[int]:
    """Get every bridge_log() destination channel ID from config."""
    cfg = _load()
    return cfg.BOT.get("log_channel_ids", []) if cfg else []


def get_command_id(category: str, command: str) -> int:
    """
    Get a specific command ID from config.

    Args:
        category: Command category (e.g., "ai", "messaging", "interaction")
        command: Command name (e.g., "gemini", "msg", "react")

    Returns:
        The command ID (snowflake), or 0 if not found
    """
    cfg = _load()
    if not cfg:
        return 0
    return cfg.COMMANDS.get(category, {}).get(command, 0)


def get_all_commands() -> dict:
    """Get all command IDs organized by category."""
    cfg = _load()
    return cfg.COMMANDS if cfg else {}
