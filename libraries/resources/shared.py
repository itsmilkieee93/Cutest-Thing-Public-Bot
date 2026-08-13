import discord
from discord import app_commands
import os
import re
import json
import asyncio
import aiosqlite
from datetime import datetime

import importlib.util as _ilu

# config_loader.py lives in libraries/, one level up from resources/.
# Loaded by explicit path (not sys.path injection) so we don't disturb
# import resolution for other modules like my_youtube_channel.py.
_config_loader_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "config_loader.py"
)
_spec = _ilu.spec_from_file_location("config_loader", _config_loader_path)
config_loader = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(config_loader)

# =====================================================================
# 🌸 BRIDGE PROTOCOL: GLOBAL RESOURCES
# =====================================================================
msg_cache = {}
BASE_GEMINI_DIR = "gemini/memory"
BASE_GROQ_DIR   = "groq/memory"
BASE_GUILD_DIR  = "cache/guild_data"
joke_emojis = ["😂", "🤣", "💀", "😭", "🥀", "🔥", "😅", "😮", "💨", "😆", "🫠", "🤭", "💔", "😔", "🤞"]
quote_emojis = ["😊", "✨", "💖", "🙌", "🌸", "🍀", "🌟", "🕊️", "📖", "🌿", "💫", "🌈", "🕯️", "💎", "🎀", "🥰", "💞", "🥺", "🥹"]


# =====================================================================
# 🛠️ SYSTEM UTILITIES
# =====================================================================
def _load_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except:
        return ""


async def bridge_log(interaction: discord.Interaction, command_name: str, arguments: dict | str = None, output: str = ""):
    """Detailed logger with structured command syntax, User PFP, and Server/Channel info."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_id    = interaction.user.id
    guild_name = interaction.guild.name if interaction.guild else "Direct Messages"
    guild_id   = interaction.guild.id  if interaction.guild else "N/A"
    channel_name = interaction.channel.name if hasattr(interaction.channel, 'name') else "DM"
    channel_id   = interaction.channel.id  if interaction.channel else "N/A"

    syntax_parts = [f"/{command_name}"]
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if value: syntax_parts.append(f"{key}:{value}")
    elif isinstance(arguments, str):
        syntax_parts.append(arguments)

    detailed_command = " ".join(syntax_parts)
    output_preview = f"{str(output)[:300]}..." if len(str(output)) > 300 else str(output)

    log_entry = (
        f"\n--- [BRIDGE LOG @ {timestamp}] ---\n"
        f"👤 USER ID:   {user_id}\n"
        f"🌐 SRV:       {guild_name} ({guild_id})\n"
        f"📺 CHAN:      #{channel_name} ({channel_id})\n"
        f"🛰️ CMD:       {detailed_command}\n"
        f"📤 OUTPUT:    {output_preview}\n"
        f"{'-'*40}\n"
    )

    try:
        if not os.path.exists("logs"):
            os.makedirs("logs")
        with open("logs/command_track.log", "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        print(f"❌ File Log Error: {e}")

    log_channel_ids = config_loader.get_log_channel_ids()

    if log_channel_ids:
        embed = discord.Embed(
            title="Command Executed! 🌟",
            description=f"**Full Command:** `{detailed_command}`",
            color=0xff0dd7,
            timestamp=datetime.now()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(
            name="👤 User Information",
            value=f"**Name:** {interaction.user} ({interaction.user.mention})\n**ID:** `{user_id}`",
            inline=True
        )
        embed.add_field(
            name="🌐 Location",
            value=(
                f"**Server:** {guild_name}\n"
                f"**Channel:** `#{channel_name}`\n"
                f"**Srv ID:** `{guild_id}`\n"
                f"**Chan ID:** `{channel_id}`"
            ),
            inline=True
        )
        embed.add_field(name="📤 Result", value=f"\n{output_preview or 'Executed successfully.'}\n", inline=False)
        embed.set_footer(text=f"Requested by {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

        for log_channel_id in log_channel_ids:
            channel = interaction.client.get_channel(log_channel_id)
            if not channel:
                print(f"❌ Discord Log Error: channel {log_channel_id} not found/cached.")
                continue
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"❌ Discord Log Error ({log_channel_id}): {e}")


# =====================================================================
# 🧠 MEMORY MANAGEMENT
# =====================================================================
def load_gemini_memory(model_id, username, user_id):
    clean_user = re.sub(r'[^\w\-]', '_', str(username))
    mem_folder = os.path.join(BASE_GEMINI_DIR, str(model_id))
    mem_path   = os.path.join(mem_folder, f"chat_{clean_user}_{user_id}.json")
    if os.path.exists(mem_path):
        try:
            with open(mem_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []


def save_gemini_memory(model_id, username, user_id, history):
    clean_user = re.sub(r'[^\w\-]', '_', str(username))
    mem_folder = os.path.join(BASE_GEMINI_DIR, str(model_id))
    mem_path   = os.path.join(mem_folder, f"chat_{clean_user}_{user_id}.json")
    os.makedirs(mem_folder, exist_ok=True)
    try:
        with open(mem_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Disk Error: {e}")



# 🌸 Per-guild Groq memory — one SQLite file per guild instead of one
# shared bot_history.db for everyone:
#   groq/memory/{guild_id}/memory.db
# DMs (no guild) use guild_id=0 as their own folder/bucket, e.g.
#   groq/memory/0/memory.db
# user_id is always the sender's real Discord snowflake ID
# (message.author.id / interaction.user.id), never a display name or
# username, so lookups stay stable even if someone changes their
# Discord username later.

# 🌸 Guards the ONE-TIME "PRAGMA journal_mode=WAL" call per DB file per
# process. journal_mode=WAL is persisted into the database file itself
# once set, so this only needs to actually run again after a fresh
# db-file creation — but re-asserting it cheaply on every fresh connection
# is fine too; this set just avoids doing it redundantly on every single
# load/save call. Keyed per guild_id since each guild now has its own file.
_wal_enabled_guilds = set()


def _groq_memory_dir(guild_id: int) -> str:
    """🌸 Directory holding this guild's memory.db — guild_id=0 for DMs."""
    return os.path.join(BASE_GROQ_DIR, str(guild_id or 0))


def _groq_memory_db_path(guild_id: int) -> str:
    """🌸 Path to groq/memory/{guild_id}/memory.db."""
    return os.path.join(_groq_memory_dir(guild_id), "memory.db")


async def _get_groq_db(guild_id: int = 0) -> aiosqlite.Connection:
    """🌸 Opens a fresh aiosqlite connection to this guild's
    groq/memory/{guild_id}/memory.db, creating the table/index and
    enabling WAL mode (write-ahead logging) the first time this process
    touches that file. WAL lets reads and writes happen concurrently
    instead of locking the whole file on every write — much friendlier
    for a Discord bot that may be handling several mentions/replies in
    parallel across servers.
    """
    guild_id = guild_id or 0
    guild_dir = _groq_memory_dir(guild_id)
    os.makedirs(guild_dir, exist_ok=True)

    conn = await aiosqlite.connect(_groq_memory_db_path(guild_id))

    if guild_id not in _wal_enabled_guilds:
        # journal_mode=WAL persists in the db file itself after the first
        # successful call, but it's harmless (and fast) to re-assert it.
        await conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL is the standard/recommended synchronous level to pair
        # with WAL — still crash-safe, just skips some fsyncs that FULL
        # would do, which is the whole point of using WAL for concurrency.
        await conn.execute("PRAGMA synchronous=NORMAL")
        _wal_enabled_guilds.add(guild_id)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            model_name TEXT NOT NULL,
            history_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 🌸 Within a single guild's memory.db, history is keyed on user_id
    # alone (one row per user) — the guild separation now comes from
    # which FILE you're connected to, not from a guild_id column.
    # model_name is kept as a column just to record whichever model
    # most recently wrote to that user's row (handy for debugging),
    # it's not part of the lookup key.
    await conn.execute("DROP INDEX IF EXISTS idx_user_model")
    try:
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_shared
            ON chat_history(user_id)
        """)
    except Exception as e:
        # This only fails if this guild's DB somehow has old per-model
        # duplicate rows for the same user_id left over from before
        # memory was shared cross-model. Safe to ignore on a fresh file.
        print(f"⚠️ Couldn't create memory index for guild {guild_id} ({e}).")
    await conn.commit()
    return conn


async def _init_groq_db(guild_id: int = 0):
    """🌸 Initialize a guild's groq/memory/{guild_id}/memory.db with the
    chat_history table (and WAL mode) if it doesn't exist yet. Safe to
    call from cog_load — await shared._init_groq_db(guild_id) initializes
    that guild's memory DB on startup (or pass nothing / 0 for the DM
    memory DB).
    """
    conn = await _get_groq_db(guild_id)
    await conn.close()


async def save_groq_memory(model_dir: str, username: str, user_id: int, history: list, guild_id: int = 0):
    """🌸 Save history into this guild's own memory.db — one row per
    user_id, scoped entirely by which guild's DB file this connects to.
    Pass guild_id=0 (the default) for DMs, so DM history lives in its
    own groq/memory/0/memory.db separate from every server. model_dir
    is recorded (for debugging), but history is still cross-model
    shared within that guild."""
    guild_id = guild_id or 0
    conn = await _get_groq_db(guild_id)
    try:
        await conn.execute("""
            INSERT INTO chat_history (user_id, username, model_name, history_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                model_name=excluded.model_name,
                history_json=excluded.history_json,
                updated_at=CURRENT_TIMESTAMP
        """, (user_id, username, model_dir, json.dumps(history)))
        await conn.commit()
    except Exception as e:
        print(f"❌ Groq memory save error for {username} ({user_id}) in guild {guild_id}: {e}")
    finally:
        await conn.close()


async def load_groq_memory(user_id: int, guild_id: int = 0) -> list:
    """🌸 Load this user's Groq memory from a single guild's own
    memory.db (or groq/memory/0/memory.db for DMs). What they said in
    one server no longer shows up in another — each guild's history
    lives in a completely separate SQLite file."""
    guild_id = guild_id or 0
    conn = await _get_groq_db(guild_id)
    try:
        cur = await conn.execute(
            "SELECT history_json FROM chat_history WHERE user_id = ?",
            (user_id,)
        )
        row = await cur.fetchone()
        if row:
            return json.loads(row[0])
        return []
    except Exception as e:
        print(f"❌ Groq memory load error for user {user_id} in guild {guild_id}: {e}")
        return []
    finally:
        await conn.close()


async def migrate_old_shared_memory(default_guild_id: int = 0):
    """🌸 One-time migration helper for upgrading from the old single
    groq/memory/bot_history.db (shared across every guild) to the new
    per-guild groq/memory/{guild_id}/memory.db layout. Reads every row
    out of the old file (if it still exists) and copies it into
    default_guild_id's memory.db — pick 0 to land it in the DM bucket,
    or your main server's guild_id if that's where most of that old
    history actually happened. Old rows are just copied, not moved, so
    the legacy file is left untouched on disk in case you want to run
    this again for a different guild_id. Call this manually once, e.g.
    `await shared.migrate_old_shared_memory(YOUR_GUILD_ID)` — it is NOT
    called automatically anywhere.
    """
    old_path = os.path.join(BASE_GROQ_DIR, "bot_history.db")
    if not os.path.exists(old_path):
        print("✅ No legacy groq/memory/bot_history.db found — nothing to migrate.")
        return

    old_conn = await aiosqlite.connect(old_path)
    try:
        cur = await old_conn.execute(
            "SELECT user_id, username, model_name, history_json FROM chat_history"
        )
        rows = await cur.fetchall()
    finally:
        await old_conn.close()

    for user_id, username, model_name, history_json in rows:
        try:
            history = json.loads(history_json)
        except Exception:
            history = []
        await save_groq_memory(model_name, username, user_id, history, default_guild_id)

    print(f"✅ Migrated {len(rows)} old chat_history row(s) into "
          f"groq/memory/{default_guild_id or 0}/memory.db")


# =====================================================================
# 🏰 GUILD CACHE — SEPARATED: metadata.db, roles.db, channels.db
# =====================================================================
# 🌸 Directory structure:
# cache/guild_data/{guild_id}/
#   ├── metadata.db    (guild_info table)
#   ├── roles.db       (roles + member_roles tables)
#   └── channels.db    (channels table + members table for channel context)

def _guild_db_dir(guild_id: int) -> str:
    """🌸 Get the directory for a guild's separated DB files."""
    return os.path.join(BASE_GUILD_DIR, str(guild_id))


def _guild_metadata_db_path(guild_id: int) -> str:
    """🌸 Path to guild metadata database."""
    return os.path.join(_guild_db_dir(guild_id), "metadata.db")


def _guild_roles_db_path(guild_id: int) -> str:
    """🌸 Path to guild roles database."""
    return os.path.join(_guild_db_dir(guild_id), "roles.db")


def _guild_channels_db_path(guild_id: int) -> str:
    """🌸 Path to guild channels database."""
    return os.path.join(_guild_db_dir(guild_id), "channels.db")


async def _get_metadata_db(guild_id: int) -> aiosqlite.Connection:
    """🌸 Opens connection to metadata.db (guild_info table)."""
    guild_dir = _guild_db_dir(guild_id)
    os.makedirs(guild_dir, exist_ok=True)
    
    conn = await aiosqlite.connect(_guild_metadata_db_path(guild_id))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    
    # ── guild_info: single-row table holding top-level guild metadata ──
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_info (
            id                      INTEGER PRIMARY KEY,
            name                    TEXT,
            owner_id                INTEGER,
            created_at              TEXT,
            member_count            INTEGER,
            description             TEXT,
            icon                    TEXT,
            banner                  TEXT,
            preferred_locale        TEXT,
            verification_level      TEXT,
            default_notifications   TEXT,
            explicit_content_filter TEXT,
            mfa_level               TEXT,
            nsfw_level              TEXT,
            cached_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    return conn


async def _get_roles_db(guild_id: int) -> aiosqlite.Connection:
    """🌸 Opens connection to roles.db (roles + member_roles tables)."""
    guild_dir = _guild_db_dir(guild_id)
    os.makedirs(guild_dir, exist_ok=True)
    
    conn = await aiosqlite.connect(_guild_roles_db_path(guild_id))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    
    # ── roles ────────────────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id           INTEGER PRIMARY KEY,
            name         TEXT,
            color        INTEGER,
            hoist        INTEGER,
            position     INTEGER,
            permissions  TEXT,
            managed      INTEGER,
            mentionable  INTEGER,
            icon         TEXT
        )
    """)
    
    # ── members ──────────────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id            INTEGER PRIMARY KEY,
            username      TEXT,
            display_name  TEXT,
            discriminator TEXT,
            avatar        TEXT,
            bot           INTEGER,
            system        INTEGER,
            joined_at     TEXT,
            nickname      TEXT,
            pending       INTEGER
        )
    """)
    
    # ── member_roles: many-to-many join table (a member can have N roles) ──
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS member_roles (
            member_id INTEGER NOT NULL,
            role_id   INTEGER NOT NULL,
            PRIMARY KEY (member_id, role_id)
        )
    """)
    
    return conn


async def _get_channels_db(guild_id: int) -> aiosqlite.Connection:
    """🌸 Opens connection to channels.db (channels table)."""
    guild_dir = _guild_db_dir(guild_id)
    os.makedirs(guild_dir, exist_ok=True)
    
    conn = await aiosqlite.connect(_guild_channels_db_path(guild_id))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    
    # ── channels ─────────────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id                             INTEGER PRIMARY KEY,
            name                           TEXT,
            type                           TEXT,
            position                       INTEGER,
            parent_id                      INTEGER,
            nsfw                           INTEGER,
            topic                          TEXT,
            slowmode_delay                 INTEGER,
            default_auto_archive_duration  INTEGER,
            bitrate                        INTEGER,
            user_limit                     INTEGER
        )
    """)
    
    return conn


async def save_guild_db(guild_data: dict):
    """
    🌸 Persist a guild_data dict into THREE separated databases:
    - metadata.db: guild_info
    - roles.db: roles + members + member_roles
    - channels.db: channels
    
    Usage:
        from shared import save_guild_db
        await save_guild_db(guild_data)
    """
    guild_id = guild_data["id"]
    
    # ══════════════════════════════════════════════════════════════════
    # 1️⃣ METADATA DATABASE
    # ══════════════════════════════════════════════════════════════════
    conn_meta = await _get_metadata_db(guild_id)
    try:
        # ── guild_info (upsert single row) ──────────────────────────
        await conn_meta.execute("""
            INSERT INTO guild_info (
                id, name, owner_id, created_at, member_count, description,
                icon, banner, preferred_locale, verification_level,
                default_notifications, explicit_content_filter, mfa_level,
                nsfw_level, cached_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                owner_id=excluded.owner_id,
                created_at=excluded.created_at,
                member_count=excluded.member_count,
                description=excluded.description,
                icon=excluded.icon,
                banner=excluded.banner,
                preferred_locale=excluded.preferred_locale,
                verification_level=excluded.verification_level,
                default_notifications=excluded.default_notifications,
                explicit_content_filter=excluded.explicit_content_filter,
                mfa_level=excluded.mfa_level,
                nsfw_level=excluded.nsfw_level,
                cached_at=CURRENT_TIMESTAMP
        """, (
            guild_data.get("id"), guild_data.get("name"), guild_data.get("owner_id"),
            guild_data.get("created_at"), guild_data.get("member_count"),
            guild_data.get("description"), guild_data.get("icon"), guild_data.get("banner"),
            guild_data.get("preferred_locale"), guild_data.get("verification_level"),
            guild_data.get("default_notifications"), guild_data.get("explicit_content_filter"),
            json.dumps(guild_data.get("mfa_level")) if isinstance(guild_data.get("mfa_level"), (list, dict)) else str(guild_data.get("mfa_level")),
            guild_data.get("nsfw_level"),
        ))
        await conn_meta.commit()
        print(f"💾 [METADATA] Saved {guild_data.get('name')} ({guild_id})")
    except Exception as e:
        print(f"❌ Metadata DB save error ({guild_id}): {e}")
    finally:
        await conn_meta.close()
    
    # ══════════════════════════════════════════════════════════════════
    # 2️⃣ ROLES DATABASE
    # ══════════════════════════════════════════════════════════════════
    conn_roles = await _get_roles_db(guild_id)
    try:
        # ── roles: clear + reinsert ──────────────────────────────────
        await conn_roles.execute("DELETE FROM roles")
        for role in guild_data.get("roles", []):
            await conn_roles.execute("""
                INSERT INTO roles (id, name, color, hoist, position, permissions, managed, mentionable, icon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                role.get("id"), role.get("name"), role.get("color"),
                int(bool(role.get("hoist"))), role.get("position"),
                role.get("permissions"), int(bool(role.get("managed"))),
                int(bool(role.get("mentionable"))), role.get("icon"),
            ))

        # ── members + member_roles: clear + reinsert ─────────────────
        await conn_roles.execute("DELETE FROM members")
        await conn_roles.execute("DELETE FROM member_roles")
        for m in guild_data.get("members", []):
            await conn_roles.execute("""
                INSERT INTO members (
                    id, username, display_name, discriminator, avatar,
                    bot, system, joined_at, nickname, pending
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                m.get("id"), m.get("username"), m.get("display_name"),
                m.get("discriminator"), m.get("avatar"),
                int(bool(m.get("bot"))), int(bool(m.get("system"))),
                m.get("joined_at"), m.get("nickname"), int(bool(m.get("pending"))),
            ))
            for role_id in m.get("roles", []):
                await conn_roles.execute(
                    "INSERT OR IGNORE INTO member_roles (member_id, role_id) VALUES (?, ?)",
                    (m.get("id"), role_id),
                )

        await conn_roles.commit()
        print(f"💾 [ROLES] Saved {len(guild_data.get('roles', []))} roles & {len(guild_data.get('members', []))} members")
    except Exception as e:
        print(f"❌ Roles DB save error ({guild_id}): {e}")
    finally:
        await conn_roles.close()
    
    # ══════════════════════════════════════════════════════════════════
    # 3️⃣ CHANNELS DATABASE
    # ══════════════════════════════════════════════════════════════════
    conn_channels = await _get_channels_db(guild_id)
    try:
        # ── channels: clear + reinsert ───────────────────────────────
        await conn_channels.execute("DELETE FROM channels")
        for ch in guild_data.get("channels", []):
            await conn_channels.execute("""
                INSERT INTO channels (
                    id, name, type, position, parent_id, nsfw, topic,
                    slowmode_delay, default_auto_archive_duration, bitrate, user_limit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ch.get("id"), ch.get("name"), ch.get("type"), ch.get("position"),
                ch.get("parent_id"), int(bool(ch.get("nsfw"))), ch.get("topic"),
                ch.get("slowmode_delay"), ch.get("default_auto_archive_duration"),
                ch.get("bitrate"), ch.get("user_limit"),
            ))

        await conn_channels.commit()
        print(f"💾 [CHANNELS] Saved {len(guild_data.get('channels', []))} channels")
    except Exception as e:
        print(f"❌ Channels DB save error ({guild_id}): {e}")
    finally:
        await conn_channels.close()
    
    print(f"✅ Guild {guild_data.get('name')} ({guild_id}) saved to all 3 databases\n")


async def load_guild_db(guild_id: int) -> dict:
    """
    🌸 Reads all three separated databases (metadata, roles, channels)
    back into the same dict shape used everywhere else.
    Returns {} if the .db files don't exist yet for this guild.
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return {}

    guild_data = {}
    
    # ══════════════════════════════════════════════════════════════════
    # 1️⃣ LOAD METADATA
    # ══════════════════════════════════════════════════════════════════
    conn_meta = await _get_metadata_db(guild_id)
    try:
        cur = await conn_meta.execute("SELECT * FROM guild_info WHERE id=?", (guild_id,))
        row = await cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            guild_data = dict(zip(cols, row))
    except Exception as e:
        print(f"❌ Metadata load error ({guild_id}): {e}")
    finally:
        await conn_meta.close()
    
    # ══════════════════════════════════════════════════════════════════
    # 2️⃣ LOAD ROLES & MEMBERS
    # ══════════════════════════════════════════════════════════════════
    conn_roles = await _get_roles_db(guild_id)
    try:
        cur = await conn_roles.execute("SELECT id, name, color, hoist, position, permissions, managed, mentionable, icon FROM roles ORDER BY position DESC")
        rows = await cur.fetchall()
        guild_data["roles"] = [
            {
                "id": r[0], "name": r[1], "color": r[2], "hoist": bool(r[3]),
                "position": r[4], "permissions": r[5], "managed": bool(r[6]),
                "mentionable": bool(r[7]), "icon": r[8],
            }
            for r in rows
        ]

        cur = await conn_roles.execute("""
            SELECT id, username, display_name, discriminator, avatar,
                   bot, system, joined_at, nickname, pending
            FROM members
        """)
        member_rows = await cur.fetchall()

        cur = await conn_roles.execute("SELECT member_id, role_id FROM member_roles")
        role_map = {}
        for member_id, role_id in await cur.fetchall():
            role_map.setdefault(member_id, []).append(role_id)

        guild_data["members"] = [
            {
                "id": r[0], "username": r[1], "display_name": r[2],
                "discriminator": r[3], "avatar": r[4], "bot": bool(r[5]),
                "system": bool(r[6]), "joined_at": r[7],
                "roles": role_map.get(r[0], []), "nickname": r[8],
                "pending": bool(r[9]),
            }
            for r in member_rows
        ]
    except Exception as e:
        print(f"❌ Roles load error ({guild_id}): {e}")
        guild_data["roles"] = []
        guild_data["members"] = []
    finally:
        await conn_roles.close()
    
    # ══════════════════════════════════════════════════════════════════
    # 3️⃣ LOAD CHANNELS
    # ══════════════════════════════════════════════════════════════════
    conn_channels = await _get_channels_db(guild_id)
    try:
        cur = await conn_channels.execute("""
            SELECT id, name, type, position, parent_id, nsfw, topic,
                   slowmode_delay, default_auto_archive_duration, bitrate, user_limit
            FROM channels ORDER BY position ASC
        """)
        rows = await cur.fetchall()
        guild_data["channels"] = [
            {
                "id": r[0], "name": r[1], "type": r[2], "position": r[3],
                "parent_id": r[4], "nsfw": bool(r[5]), "topic": r[6],
                "slowmode_delay": r[7], "default_auto_archive_duration": r[8],
                "bitrate": r[9], "user_limit": r[10],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"❌ Channels load error ({guild_id}): {e}")
        guild_data["channels"] = []
    finally:
        await conn_channels.close()
    
    return guild_data


async def get_members_with_role(guild_id: int, role_name: str) -> list[dict]:
    """
    🌸 Query roles.db to find all members who have a specific role.
    Returns a list of member dicts with id, username, display_name, roles.
    
    Usage:
        members = await get_members_with_role(1479158469981376656, "Senior Moderator")
        for m in members:
            print(f"  {m['display_name']} (@{m['username']})")
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return []

    conn = await _get_roles_db(guild_id)
    try:
        # Find role_id by name
        cur = await conn.execute("SELECT id FROM roles WHERE LOWER(name) = LOWER(?)", (role_name,))
        role_row = await cur.fetchone()
        if not role_row:
            return []
        role_id = role_row[0]

        # Find all members with this role
        cur = await conn.execute("""
            SELECT DISTINCT m.id, m.username, m.display_name, m.discriminator, m.avatar, m.bot
            FROM members m
            JOIN member_roles mr ON m.id = mr.member_id
            WHERE mr.role_id = ?
            ORDER BY m.display_name ASC
        """, (role_id,))
        rows = await cur.fetchall()

        # Get all roles for each member
        members = []
        for row in rows:
            member_id = row[0]
            cur = await conn.execute(
                "SELECT role_id FROM member_roles WHERE member_id = ?",
                (member_id,)
            )
            role_ids = [r[0] for r in await cur.fetchall()]
            members.append({
                "id": row[0],
                "username": row[1],
                "display_name": row[2],
                "discriminator": row[3],
                "avatar": row[4],
                "bot": bool(row[5]),
                "roles": role_ids,
            })

        return members
    except Exception as e:
        print(f"❌ Query error (get_members_with_role): {e}")
        return []
    finally:
        await conn.close()


async def get_member_roles(guild_id: int, member_id: int) -> list[dict]:
    """
    🌸 Get all roles for a specific member in a guild.
    Returns a list of role dicts with id, name, color, position, etc.
    
    Usage:
        roles = await get_member_roles(1479158469981376656, 1213450996391485471)
        for r in roles:
            print(f"  {r['name']} (color: {r['color']})")
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return []

    conn = await _get_roles_db(guild_id)
    try:
        cur = await conn.execute("""
            SELECT r.id, r.name, r.color, r.hoist, r.position, r.permissions, r.managed, r.mentionable, r.icon
            FROM roles r
            JOIN member_roles mr ON r.id = mr.role_id
            WHERE mr.member_id = ?
            ORDER BY r.position DESC
        """, (member_id,))
        rows = await cur.fetchall()

        return [
            {
                "id": r[0],
                "name": r[1],
                "color": r[2],
                "hoist": bool(r[3]),
                "position": r[4],
                "permissions": r[5],
                "managed": bool(r[6]),
                "mentionable": bool(r[7]),
                "icon": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"❌ Query error (get_member_roles): {e}")
        return []
    finally:
        await conn.close()


async def get_role_info(guild_id: int, role_name: str) -> dict:
    """
    🌸 Get details about a specific role (by name).
    Returns role dict with id, name, color, position, member_count, etc.
    
    Usage:
        role = await get_role_info(1479158469981376656, "Senior Moderator")
        print(f"  {role['name']}: {role['member_count']} members")
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return {}

    conn = await _get_roles_db(guild_id)
    try:
        cur = await conn.execute(
            "SELECT id, name, color, hoist, position, permissions, managed, mentionable, icon FROM roles WHERE name = ?",
            (role_name,)
        )
        row = await cur.fetchone()
        if not row:
            return {}

        role_id = row[0]
        cur = await conn.execute(
            "SELECT COUNT(*) FROM member_roles WHERE role_id = ?",
            (role_id,)
        )
        member_count = (await cur.fetchone())[0]

        return {
            "id": row[0],
            "name": row[1],
            "color": row[2],
            "hoist": bool(row[3]),
            "position": row[4],
            "permissions": row[5],
            "managed": bool(row[6]),
            "mentionable": bool(row[7]),
            "icon": row[8],
            "member_count": member_count,
        }
    except Exception as e:
        print(f"❌ Query error (get_role_info): {e}")
        return {}
    finally:
        await conn.close()


async def list_all_roles(guild_id: int) -> list[dict]:
    """
    🌸 Get all roles in a guild with member counts.
    Sorted by position (highest first).
    
    Usage:
        roles = await list_all_roles(1479158469981376656)
        for r in roles:
            print(f"  {r['name']}: {r['member_count']} members")
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return []

    conn = await _get_roles_db(guild_id)
    try:
        cur = await conn.execute(
            "SELECT id, name, color, hoist, position, permissions, managed, mentionable, icon FROM roles ORDER BY position DESC"
        )
        roles_rows = await cur.fetchall()

        roles = []
        for row in roles_rows:
            role_id = row[0]
            cur = await conn.execute(
                "SELECT COUNT(*) FROM member_roles WHERE role_id = ?",
                (role_id,)
            )
            member_count = (await cur.fetchone())[0]
            roles.append({
                "id": row[0],
                "name": row[1],
                "color": row[2],
                "hoist": bool(row[3]),
                "position": row[4],
                "permissions": row[5],
                "managed": bool(row[6]),
                "mentionable": bool(row[7]),
                "icon": row[8],
                "member_count": member_count,
            })

        return roles
    except Exception as e:
        print(f"❌ Query error (list_all_roles): {e}")
        return []
    finally:
        await conn.close()


async def get_guild_metadata(guild_id: int) -> dict:
    """
    🌸 Get raw guild_info row from metadata.db — name, owner_id, created_at,
    member_count, description, icon, banner, verification_level, etc.
    Returns {} if nothing cached yet.

    Usage:
        meta = await get_guild_metadata(guild_id)
        print(meta["created_at"])  # ISO string, e.g. "2019-03-14T10:22:31+00:00"
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return {}

    conn = await _get_metadata_db(guild_id)
    try:
        cur = await conn.execute("""
            SELECT id, name, owner_id, created_at, member_count, description,
                   icon, banner, preferred_locale, verification_level,
                   default_notifications, explicit_content_filter, mfa_level,
                   nsfw_level, cached_at
            FROM guild_info WHERE id = ?
        """, (guild_id,))
        row = await cur.fetchone()
        if not row:
            return {}

        return {
            "id": row[0], "name": row[1], "owner_id": row[2],
            "created_at": row[3], "member_count": row[4],
            "description": row[5], "icon": row[6], "banner": row[7],
            "preferred_locale": row[8], "verification_level": row[9],
            "default_notifications": row[10],
            "explicit_content_filter": row[11], "mfa_level": row[12],
            "nsfw_level": row[13], "cached_at": row[14],
        }
    except Exception as e:
        print(f"❌ Query error (get_guild_metadata): {e}")
        return {}
    finally:
        await conn.close()


async def get_guild_context_summary(guild_id: int, max_roles: int = 300, max_channels: int = 100) -> str:
    """
    🌸 SMART GUILD SUMMARY for AI — compact, token-efficient.
    Returns a formatted string safe to send to Groq without blasting tokens.
    
    - Only lists role names + member counts (not full objects)
    - Limits to top N roles by members (default 15)
    - Lists channel names + types (compact)
    - Skips hidden metadata like permissions, icons, etc.
    
    Usage:
        summary = await get_guild_context_summary(guild_id)
        # Then pass to Groq:
        # "Guild context: " + summary
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return "No guild data cached."

    # ── Load metadata from metadata.db ────────────────────────────────
    conn_meta = await _get_metadata_db(guild_id)
    cur = await conn_meta.execute("SELECT name, member_count FROM guild_info WHERE id = ?", (guild_id,))
    row = await cur.fetchone()
    await conn_meta.close()
    
    if not row:
        return "Guild data not found."
    guild_name, member_count = row

    lines = [f"📍 Server: {guild_name} ({member_count} members)"]
    lines.append("")

    # ── Load roles from roles.db ──────────────────────────────────────
    conn_roles = await _get_roles_db(guild_id)
    try:
        cur = await conn_roles.execute("""
            SELECT r.id, r.name, COUNT(mr.member_id) as member_count
            FROM roles r
            LEFT JOIN member_roles mr ON r.id = mr.role_id
            GROUP BY r.id
            ORDER BY r.position DESC
            LIMIT ?
        """, (max_roles,))
        roles_data = await cur.fetchall()

        lines.append(f"👥 **Top {len(roles_data)} Roles:**")
        for role_id, role_name, role_member_count in roles_data:
            lines.append(f"  • {role_name}: {role_member_count} members")
        lines.append("")
    finally:
        await conn_roles.close()

    # ── Load channels from channels.db ────────────────────────────────
    conn_channels = await _get_channels_db(guild_id)
    try:
        cur = await conn_channels.execute("""
            SELECT id, name, type
            FROM channels
            ORDER BY position ASC
            LIMIT ?
        """, (max_channels,))
        channels_data = await cur.fetchall()

        lines.append(f"💬 **Channels ({len(channels_data)}):**")
        for ch_id, ch_name, ch_type in channels_data:
            icon = "🔊" if ch_type == "voice" else "💬" if ch_type == "text" else "📂"
            lines.append(f"  {icon} {ch_name}")
        lines.append("")
    finally:
        await conn_channels.close()

    return "\n".join(lines)


async def get_compact_roles(guild_id: int, max_count: int = 20) -> str:
    """
    🌸 Ultra-compact role list for AI context (role_name: member_count).
    Safe to dump directly into Groq prompts.
    
    Usage:
        roles_text = await get_compact_roles(guild_id, max_count=15)
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return "No roles cached."

    conn = await _get_roles_db(guild_id)
    try:
        cur = await conn.execute("""
            SELECT r.name, COUNT(mr.member_id) as member_count
            FROM roles r
            LEFT JOIN member_roles mr ON r.id = mr.role_id
            GROUP BY r.id
            ORDER BY r.position DESC
            LIMIT ?
        """, (max_count,))
        roles = await cur.fetchall()

        lines = []
        for role_name, role_count in roles:
            lines.append(f"{role_name}: {role_count}")
        
        return "\n".join(lines) if lines else "No roles found."
    except Exception as e:
        print(f"❌ Compact roles error: {e}")
        return f"Error: {e}"
    finally:
        await conn.close()


async def get_compact_channels(guild_id: int, max_count: int | None = None) -> str:
    # Kode Anda di sini
    """
    🌸 Ultra-compact channel list for AI context (type: name).
    Safe to dump directly into Groq prompts.
    
    Usage:
        channels_text = await get_compact_channels(guild_id)
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return "No channels cached."

    conn = await _get_channels_db(guild_id)
    try:
        cur = await conn.execute("""
            SELECT name, type
            FROM channels
            ORDER BY position ASC
            LIMIT ?
        """, (max_count,))
        channels = await cur.fetchall()

        lines = []
        for ch_name, ch_type in channels:
            lines.append(f"[{ch_type}] {ch_name}")
        
        return "\n".join(lines) if lines else "No channels found."
    except Exception as e:
        print(f"❌ Compact channels error: {e}")
        return f"Error: {e}"
    finally:
        await conn.close()


async def migrate_guild_json_to_sqlite():
    """
    🌸 MIGRATION HELPER: Imports all existing cache/guild_data/guild_*.json
    files into separated .db files (metadata/roles/channels). Safe to run 
    multiple times — save_guild_db fully replaces each table's rows on every call.
    Call once after upgrading to the separated database structure.

    Usage:
        from shared import migrate_guild_json_to_sqlite
        await migrate_guild_json_to_sqlite()
    """
    if not os.path.exists(BASE_GUILD_DIR):
        print(f"❌ {BASE_GUILD_DIR} doesn't exist — nothing to migrate.")
        return

    migrated, errors = 0, 0
    for filename in os.listdir(BASE_GUILD_DIR):
        if not (filename.startswith("guild_") and filename.endswith(".json")):
            continue
        filepath = os.path.join(BASE_GUILD_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                guild_data = json.load(f)
            await save_guild_db(guild_data)
            migrated += 1
        except Exception as e:
            print(f"❌ {filename}: {e}")
            errors += 1

    print(f"\n🌸 Guild DB Migration Summary: {migrated} imported, {errors} errors")


# =====================================================================
# 🔍 AUTOCOMPLETE FUNCTIONS
# =====================================================================
async def reply_to_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        target_chan_id = (
            getattr(interaction.namespace, 'channel', None)
            or getattr(interaction.namespace, 'from_channel', None)
        )
        if target_chan_id and isinstance(target_chan_id, discord.abc.Snowflake):
            lookup_channel = interaction.client.get_channel(target_chan_id.id)
        elif isinstance(target_chan_id, str) and target_chan_id.isdigit():
            lookup_channel = interaction.client.get_channel(int(target_chan_id))
        else:
            lookup_channel = interaction.channel

        if not lookup_channel:
            return []

        choices = []

        if current.isdigit() and len(current) >= 17:
            try:
                msg = await lookup_channel.fetch_message(int(current))
                content = (msg.content or "[Media]").replace("\n", " ")
                choices.append(app_commands.Choice(
                    name=f"📌 FOUND: {msg.author.display_name}: {content}"[:95],
                    value=str(msg.id)
                ))
            except:
                pass

        async for msg in lookup_channel.history(limit=20):
            if any(choice.value == str(msg.id) for choice in choices):
                continue
            content      = (msg.content or "[Media]").replace("\n", " ")
            name_preview = f"{msg.author.display_name}: {content}"[:95]
            if not current or current.lower() in name_preview.lower():
                choices.append(app_commands.Choice(name=name_preview, value=str(msg.id)))

        return choices[:25]
    except:
        return []


async def server_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = []
    for guild in interaction.client.guilds:
        if not current or current.lower() in guild.name.lower():
            choices.append(app_commands.Choice(name=guild.name[:100], value=str(guild.id)))
    return choices[:25]


async def target_channel_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = []

    if current.isdigit() and len(current) >= 17:
        chan = interaction.client.get_channel(int(current))
        if chan and isinstance(chan, discord.TextChannel):
            choices.append(app_commands.Choice(
                name=f"📺 FOUND: #{chan.name} ({chan.guild.name})",
                value=str(chan.id)
            ))

    target_server_id = getattr(interaction.namespace, 'target_server', None)
    if target_server_id and target_server_id.isdigit():
        guild = interaction.client.get_guild(int(target_server_id))
        if guild:
            for channel in guild.text_channels:
                if any(choice.value == str(channel.id) for choice in choices):
                    continue
                if not current or current.lower() in channel.name.lower():
                    choices.append(app_commands.Choice(name=f"#{channel.name}"[:100], value=str(channel.id)))

    return choices[:25]
