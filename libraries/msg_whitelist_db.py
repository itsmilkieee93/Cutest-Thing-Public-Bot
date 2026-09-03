"""
🌸 Message Whitelist DB — aiosqlite store of server IDs allowed to use
restricted messaging commands (e.g. /msg), registered live by moderators
via /register-server. Sits alongside the static list in discord_config.py
rather than replacing it.
"""

import os
import aiosqlite

DB_PATH = "config/msg_whitelist.db"


async def _ensure_db():
    """Create the db file/table if they don't exist yet."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS whitelist (
                guild_id INTEGER PRIMARY KEY
            )
            """
        )
        await db.commit()


async def add_server(guild_id: int) -> bool:
    """Register a server ID. Returns False if it was already whitelisted."""
    await _ensure_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO whitelist (guild_id) VALUES (?)", (guild_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def is_whitelisted(guild_id: int) -> bool:
    """True if guild_id has been registered via /register-server."""
    await _ensure_db()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM whitelist WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            return await cursor.fetchone() is not None
