import discord
from discord import app_commands
import os
import re
import json
import asyncio
import difflib
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
# DMs (no guild) get their OWN per-channel bucket instead of sharing a
# single guild_id=0 file, e.g.
#   groq/memory/dm/{channel_id}/memory.db
# user_id is always the sender's real Discord snowflake ID
# (message.author.id / interaction.user.id), never a display name or
# username, so lookups stay stable even if someone changes their
# Discord username later.

# 🌸 Guards the ONE-TIME "PRAGMA journal_mode=WAL" call per DB file per
# process. journal_mode=WAL is persisted into the database file itself
# once set, so this only needs to actually run again after a fresh
# db-file creation — but re-asserting it cheaply on every fresh connection
# is fine too; this set just avoids doing it redundantly on every single
# load/save call. Keyed per bucket_key (see _groq_bucket_key) since each
# guild/DM channel now has its own file.
_wal_enabled_guilds = set()


def _groq_bucket_key(guild_id: int, channel_id: int | None) -> str:
    """🌸 Resolves which memory.db bucket a message belongs to.

    - Guild message (guild_id truthy): bucket is just str(guild_id),
      same as before — groq/memory/{guild_id}/memory.db.
    - DM (guild_id falsy): bucket is "dm/{channel_id}" —
      groq/memory/dm/{channel_id}/memory.db. channel_id is the DM
      channel's snowflake, which is stable per-user, so this is
      effectively one bucket per DM conversation.
    - DM with no channel_id passed (legacy callers that only ever
      passed guild_id=0): falls back to "dm/0" so nothing crashes,
      but new call sites should always pass the real channel_id.
    """
    if guild_id:
        return str(guild_id)
    return os.path.join("dm", str(channel_id or 0))


def _groq_memory_dir(guild_id: int, channel_id: int | None = None) -> str:
    """🌸 Directory holding this bucket's memory.db —
    groq/memory/{guild_id}/ for guilds, groq/memory/dm/{channel_id}/
    for DMs."""
    return os.path.join(BASE_GROQ_DIR, _groq_bucket_key(guild_id, channel_id))


def _groq_memory_db_path(guild_id: int, channel_id: int | None = None) -> str:
    """🌸 Path to this bucket's memory.db."""
    return os.path.join(_groq_memory_dir(guild_id, channel_id), "memory.db")


async def _get_groq_db(guild_id: int = 0, channel_id: int | None = None) -> aiosqlite.Connection:
    """🌸 Opens a fresh aiosqlite connection to this bucket's memory.db
    (groq/memory/{guild_id}/memory.db for guilds, or
    groq/memory/dm/{channel_id}/memory.db for DMs), creating the
    table/index and enabling WAL mode (write-ahead logging) the first
    time this process touches that file. WAL lets reads and writes
    happen concurrently instead of locking the whole file on every
    write — much friendlier for a Discord bot that may be handling
    several mentions/replies in parallel across servers.
    """
    bucket_key = _groq_bucket_key(guild_id, channel_id)
    bucket_dir = os.path.join(BASE_GROQ_DIR, bucket_key)
    os.makedirs(bucket_dir, exist_ok=True)

    conn = await aiosqlite.connect(os.path.join(bucket_dir, "memory.db"))

    if bucket_key not in _wal_enabled_guilds:
        # journal_mode=WAL persists in the db file itself after the first
        # successful call, but it's harmless (and fast) to re-assert it.
        await conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL is the standard/recommended synchronous level to pair
        # with WAL — still crash-safe, just skips some fsyncs that FULL
        # would do, which is the whole point of using WAL for concurrency.
        await conn.execute("PRAGMA synchronous=NORMAL")
        _wal_enabled_guilds.add(bucket_key)

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
        print(f"⚠️ Couldn't create memory index for bucket {bucket_key} ({e}).")
    await conn.commit()
    return conn


async def _init_groq_db(guild_id: int = 0, channel_id: int | None = None):
    """🌸 Initialize a bucket's memory.db with the chat_history table
    (and WAL mode) if it doesn't exist yet. Safe to call from cog_load
    — await shared._init_groq_db(guild_id) initializes that guild's
    memory DB on startup, or shared._init_groq_db(0, channel_id) for a
    specific DM channel's bucket.
    """
    conn = await _get_groq_db(guild_id, channel_id)
    await conn.close()


async def save_groq_memory(model_dir: str, username: str, user_id: int, history: list,
                            guild_id: int = 0, channel_id: int | None = None):
    """🌸 Save history into this bucket's own memory.db — one row per
    user_id, scoped entirely by which bucket's DB file this connects
    to. For guild messages, pass guild_id — history lives in
    groq/memory/{guild_id}/memory.db. For DMs, pass guild_id=0 (or
    leave it) AND channel_id — history lives in its own
    groq/memory/dm/{channel_id}/memory.db, separate from every other
    DM and every server. model_dir is recorded (for debugging), but
    history is still cross-model shared within that bucket."""
    guild_id = guild_id or 0
    conn = await _get_groq_db(guild_id, channel_id)
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
        bucket_key = _groq_bucket_key(guild_id, channel_id)
        print(f"❌ Groq memory save error for {username} ({user_id}) in bucket {bucket_key}: {e}")
    finally:
        await conn.close()


async def load_groq_memory(user_id: int, guild_id: int = 0, channel_id: int | None = None) -> list:
    """🌸 Load this user's Groq memory from a single bucket's own
    memory.db (groq/memory/{guild_id}/memory.db for a guild, or
    groq/memory/dm/{channel_id}/memory.db for a DM). What they said in
    one server — or one DM — no longer shows up anywhere else; each
    bucket's history lives in a completely separate SQLite file."""
    guild_id = guild_id or 0
    conn = await _get_groq_db(guild_id, channel_id)
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
        bucket_key = _groq_bucket_key(guild_id, channel_id)
        print(f"❌ Groq memory load error for user {user_id} in bucket {bucket_key}: {e}")
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
#   ├── members.db     (id, global_name, username, server_name, avatar_url, banner_url,
#   │                   accent_color, avatar_decoration_url —
#   │                   generated by extras/members_db.py)
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


def _guild_members_db_path(guild_id: int) -> str:
    """🌸 Path to guild members profile database (members.db)."""
    return os.path.join(_guild_db_dir(guild_id), "members.db")


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


async def _get_members_db(guild_id: int) -> aiosqlite.Connection:
    """🌸 Opens connection to members.db — one slim table of account
    identity + CDN links (see extras/members_db.py). Separate from
    roles.db's members table on purpose: that one carries roles/join
    dates/flags for role queries; this one is just who-is-who."""
    guild_dir = _guild_db_dir(guild_id)
    os.makedirs(guild_dir, exist_ok=True)

    conn = await aiosqlite.connect(_guild_members_db_path(guild_id))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id           INTEGER PRIMARY KEY,   -- account snowflake ID
            global_name  TEXT,                  -- account global display name
            username     TEXT,                  -- account username
            server_name  TEXT,                  -- name shown in this server
            avatar_url   TEXT,                  -- account avatar CDN link
            banner_url   TEXT,                  -- account banner CDN link
            accent_color TEXT,                  -- account accent colour as "#RRGGBB" (REST-only, like banners)
            avatar_decoration_url TEXT          -- avatar decoration/frame CDN link
        )
    """)
    # 🌸 MIGRATION: members.db files created before accent_color /
    # avatar_decoration_url existed keep their old 6-column table (CREATE TABLE
    # IF NOT EXISTS won't touch it) — add the missing columns in place.
    cur = await conn.execute("PRAGMA table_info(members)")
    have = {row[1] for row in await cur.fetchall()}
    for col in ("accent_color", "avatar_decoration_url"):
        if col not in have:
            await conn.execute(f"ALTER TABLE members ADD COLUMN {col} TEXT")
    await conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════════
# 🌸 USER EXTRAS CACHE (banner + accent colour, GLOBAL across guilds)
# ══════════════════════════════════════════════════════════════════
# Banners/accent colours are account-level (same in every server) but REST
# only (GET /users/{id}). Without this cache the bot re-fetched every member
# that lacked one — i.e. most people — on every boot, once PER guild, which
# is what triggered the 429 spam. checked_at also remembers "looked, nothing
# there" so those users are skipped until USER_EXTRAS_TTL expires.
USER_EXTRAS_TTL = 7 * 24 * 3600  # seconds before a user is re-checked


def _user_extras_db_path() -> str:
    # Kept next to (not inside) BASE_GUILD_DIR so per-guild directory scans
    # never trip over it.
    return os.path.join(os.path.dirname(BASE_GUILD_DIR) or ".", "user_extras.db")


async def _get_user_extras_db() -> aiosqlite.Connection:
    path = _user_extras_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_extras (
            user_id      INTEGER PRIMARY KEY,
            banner_url   TEXT,
            accent_color TEXT,
            checked_at   REAL NOT NULL
        )
    """)
    await conn.commit()
    return conn


async def load_user_extras() -> dict[int, tuple]:
    """🌸 {user_id: (banner_url, accent_color, checked_at)}. {} on error."""
    conn = await _get_user_extras_db()
    try:
        cur = await conn.execute(
            "SELECT user_id, banner_url, accent_color, checked_at FROM user_extras"
        )
        return {r[0]: (r[1], r[2], r[3]) for r in await cur.fetchall()}
    except Exception as e:
        print(f"❌ Query error (load_user_extras): {e}")
        return {}
    finally:
        await conn.close()


async def save_user_extras(rows: list[tuple]) -> None:
    """🌸 Upserts (user_id, banner_url, accent_color, checked_at) rows."""
    if not rows:
        return
    conn = await _get_user_extras_db()
    try:
        await conn.executemany(
            "INSERT OR REPLACE INTO user_extras (user_id, banner_url, accent_color, checked_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        await conn.commit()
    except Exception as e:
        print(f"❌ Failed to save user_extras: {e}")
    finally:
        await conn.close()


async def load_member_banners(guild_id: int) -> dict[int, str]:
    """🌸 {member_id: banner_url} for every row in members.db that has a
    banner — lets a regeneration keep banners it can't re-fetch from the
    gateway. {} if the file doesn't exist yet."""
    if not os.path.exists(_guild_members_db_path(guild_id)):
        return {}
    conn = await _get_members_db(guild_id)
    try:
        cur = await conn.execute("SELECT id, banner_url FROM members WHERE banner_url IS NOT NULL")
        return {row[0]: row[1] for row in await cur.fetchall()}
    except Exception as e:
        print(f"❌ Query error (load_member_banners): {e}")
        return {}
    finally:
        await conn.close()


async def members_db_ready(guild_id: int) -> bool:
    """🌸 True if members.db exists AND has at least one row — i.e. the
    profile lookups below can be trusted. Callers fall back to roles.db's
    member table when this is False (fresh install before the first guild
    sync, or a guild whose sync only ever saw the bot)."""
    if not os.path.exists(_guild_members_db_path(guild_id)):
        return False
    conn = await _get_members_db(guild_id)
    try:
        cur = await conn.execute("SELECT 1 FROM members LIMIT 1")
        return (await cur.fetchone()) is not None
    except Exception as e:
        print(f"❌ Query error (members_db_ready): {e}")
        return False
    finally:
        await conn.close()


def _profile_row_to_dict(row) -> dict:
    """members.db row → dict. `display_name` is an alias of server_name so
    callers written against roles.db's member dicts (id/username/
    display_name) keep working unchanged."""
    return {
        "id": row[0],
        "global_name": row[1],
        "username": row[2],
        "server_name": row[3],
        "display_name": row[3] or row[1] or row[2],
        "avatar_url": row[4],
        "banner_url": row[5],
        "accent_color": row[6],
        "avatar_decoration_url": row[7],
    }


_PROFILE_COLS = "id, global_name, username, server_name, avatar_url, banner_url, accent_color, avatar_decoration_url"


# 🌸 PUNCTUATION-INSENSITIVE NAME SEARCH (last-resort stage of both name
# lookups below). Keeps ONLY ASCII letters/digits, lowercased, so
# "itsmilkieee" finds "its.milkieee" and "✦ ~ Its.milkieee ~ ♡".
# Names made purely of non-ASCII characters clean to "" and skip this stage
# (the exact/LIKE stages above still handle them).
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]")


def _strip_punct(value) -> str:
    """SQLite-callable. NULL columns (None) become "" instead of raising."""
    if not isinstance(value, str):
        return ""
    return _NON_ALNUM_RE.sub("", value).lower()


async def _normalized_member_rows(conn, select_cols: str, columns: tuple, name: str) -> list:
    """Registers strip_punct on `conn` (aiosqlite's create_function is a
    coroutine) and compares strip_punct(column) with the cleaned query:
    exact cleaned match first, then cleaned substring. `select_cols` and
    `columns` are fixed constants from the two callers (never user input);
    the user's text only travels as a bound parameter and, once cleaned, can
    only contain [a-z0-9] so it needs no LIKE escaping."""
    needle = _strip_punct(name)
    if not needle:
        # Without this, the substring query would be LIKE '%%' = EVERY member.
        return []
    try:
        await conn.create_function("strip_punct", 1, _strip_punct)
    except Exception as e:
        print(f"⚠️ strip_punct registration failed, skipping normalized search: {e}")
        return []

    exact_sql = " OR ".join(f"strip_punct({c}) = ?" for c in columns)
    cur = await conn.execute(
        f"SELECT {select_cols} FROM members WHERE {exact_sql} LIMIT 25",
        (needle,) * len(columns),
    )
    rows = await cur.fetchall()
    if rows or len(needle) < 3:
        # Under 3 chars a substring match is just noise ("a" hits everyone).
        return list(rows)

    like_sql = " OR ".join(f"strip_punct({c}) LIKE ?" for c in columns)
    like = f"%{needle}%"
    cur = await conn.execute(
        f"SELECT {select_cols} FROM members WHERE {like_sql} LIMIT 25",
        (like,) * len(columns),
    )
    return list(await cur.fetchall())


# 🌸 FUZZY (TYPO) NAME SEARCH — last-resort stage of both name lookups.
# Exact → LIKE → punctuation-insensitive all need the query to be a
# substring of the real name, so a typo like "Spy Ultime" (real name
# "Spy Ultimate") slips through. This stage scores every member with
# difflib.SequenceMatcher instead.
_FUZZY_THRESHOLD = 0.60     # minimum SequenceMatcher.ratio() to count as a match
_FUZZY_TIE_MARGIN = 0.03    # scores within this of the best are treated as a tie
_FUZZY_MAX_RESULTS = 5


def _fuzzy_pick(rows, fields: tuple, name: str, threshold: float = _FUZZY_THRESHOLD) -> list:
    """Returns the best fuzzy matches for `name` among `rows`.

    `rows` must be sqlite rows opened with `conn.row_factory = aiosqlite.Row`
    so columns are read by label (row["username"]) — no positional tuple
    indexing. Each name column is scored twice (lowercased as-is, and with
    punctuation/emoji stripped via _strip_punct) and the higher score wins.
    Only the top scorer (plus near-ties) is returned, so callers keep the
    0 = not found / 1 = resolved / 2+ = ambiguous contract.
    """
    needle = " ".join(str(name).lower().split())
    needle_flat = _strip_punct(name)
    if len(needle_flat) < 3:
        return []  # 1-2 chars: nearly every name looks "similar", pure noise

    scored = []
    for row in rows:
        best = 0.0
        for field in fields:
            value = row[field]
            if not isinstance(value, str) or not value.strip():
                continue
            cand = " ".join(value.lower().split())
            best = max(best, difflib.SequenceMatcher(None, needle, cand).ratio())
            cand_flat = _strip_punct(value)
            if cand_flat:
                best = max(
                    best,
                    difflib.SequenceMatcher(None, needle_flat, cand_flat).ratio(),
                )
        if best >= threshold:
            scored.append((best, row))

    if not scored:
        return []
    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[0][0]
    return [r for sc, r in scored if top - sc <= _FUZZY_TIE_MARGIN][:_FUZZY_MAX_RESULTS]


async def find_member_profile_by_id(guild_id: int, member_id: int) -> list[dict]:
    """🌸 Exact snowflake lookup in members.db. [] or one profile dict
    (id, global_name, username, server_name, display_name, avatar_url,
    banner_url). Use members_db_ready() first to decide members.db vs
    the roles.db fallback."""
    if not member_id or not os.path.exists(_guild_members_db_path(guild_id)):
        return []
    conn = await _get_members_db(guild_id)
    try:
        cur = await conn.execute(f"SELECT {_PROFILE_COLS} FROM members WHERE id = ?", (int(member_id),))
        row = await cur.fetchone()
        return [_profile_row_to_dict(row)] if row else []
    except Exception as e:
        print(f"❌ Query error (find_member_profile_by_id): {e}")
        return []
    finally:
        await conn.close()


async def find_member_profile_by_name(guild_id: int, name: str) -> list[dict]:
    """🌸 Name lookup in members.db across ALL THREE names a person can go
    by — username, global_name and server_name (the per-server nickname).
    Same convention as find_member_by_name: exact case-insensitive match
    first, then a substring match (with LIKE's _ and % escaped so a literal
    underscore in a username isn't read as a wildcard); callers treat
    0 = not found, 1 = resolved, 2+ = ambiguous."""
    if not name or not os.path.exists(_guild_members_db_path(guild_id)):
        return []
    conn = await _get_members_db(guild_id)
    conn.row_factory = aiosqlite.Row  # label access for the fuzzy stage; r[0] still works
    try:
        cur = await conn.execute(
            f"""
            SELECT {_PROFILE_COLS} FROM members
            WHERE LOWER(username) = LOWER(?)
               OR LOWER(global_name) = LOWER(?)
               OR LOWER(server_name) = LOWER(?)
            """,
            (name, name, name),
        )
        rows = await cur.fetchall()
        if not rows:
            escaped = name.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
            like = f"%{escaped}%"
            cur = await conn.execute(
                f"""
                SELECT {_PROFILE_COLS} FROM members
                WHERE username    LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR global_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR server_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                """,
                (like, like, like),
            )
            rows = await cur.fetchall()
        if not rows:
            # 🌸 3rd stage: ignore punctuation / spaces / emoji decorations
            rows = await _normalized_member_rows(
                conn, _PROFILE_COLS, ("username", "global_name", "server_name"), name,
            )
        if not rows:
            # 🌸 4th stage: typo safety net (fuzzy match, threshold 0.60).
            cur = await conn.execute(f"SELECT {_PROFILE_COLS} FROM members")
            all_rows = await cur.fetchall()
            rows = _fuzzy_pick(all_rows, ("username", "global_name", "server_name"), name)
        return [_profile_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"❌ Query error (find_member_profile_by_name): {e}")
        return []
    finally:
        await conn.close()


async def save_members_db(guild_id: int, rows: list[dict], expected_total: int | None = None) -> bool:
    """🌸 Replaces the contents of members.db with `rows` (dicts with id,
    global_name, username, server_name, avatar_url, banner_url, accent_color,
    avatar_decoration_url) in ONE
    transaction — readers never see a half-written table. Returns True if
    written.

    BANNERS: the gateway never supplies user banners, so a row arriving with
    banner_url=None keeps whatever banner this file already stored for that
    member (found earlier by an opt-in REST fetch) instead of wiping it.

    SAFETY: if the caller says the guild has `expected_total` members but
    `rows` holds under 10% of that (the Server Members Intent is off, or
    chunking failed, so the cache only knows the bot itself) AND the file
    already holds more rows than that, the write is SKIPPED so one bad
    sync can't wipe a good cache. Returns False in that case.
    """
    conn = await _get_members_db(guild_id)
    try:
        if expected_total and len(rows) < expected_total * 0.1:
            cur = await conn.execute("SELECT COUNT(*) FROM members")
            existing = (await cur.fetchone())[0]
            if existing > len(rows):
                print(
                    f"⚠️ [MEMBERS] Only {len(rows)}/{expected_total} members available for guild "
                    f"{guild_id} (members intent off or chunk failed?) — keeping the {existing} cached rows."
                )
                return False

        # Carry over banners + accent colours found on earlier runs (before the
        # table is cleared) — both are REST-only, the gateway never sends them.
        # The avatar decoration is NOT carried over: the gateway sends it on
        # every sync, so a missing one means the user removed it.
        cur = await conn.execute(
            "SELECT id, banner_url, accent_color FROM members "
            "WHERE banner_url IS NOT NULL OR accent_color IS NOT NULL"
        )
        old_extra = {row[0]: (row[1], row[2]) for row in await cur.fetchall()}
        for r in rows:
            old_banner, old_accent = old_extra.get(r.get("id"), (None, None))
            if not r.get("banner_url"):
                r["banner_url"] = old_banner
            if not r.get("accent_color"):
                r["accent_color"] = old_accent

        await conn.execute("DELETE FROM members")
        await conn.executemany(
            """
            INSERT OR REPLACE INTO members
                (id, global_name, username, server_name, avatar_url, banner_url,
                 accent_color, avatar_decoration_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (r.get("id"), r.get("global_name"), r.get("username"),
                 r.get("server_name"), r.get("avatar_url"), r.get("banner_url"),
                 r.get("accent_color"), r.get("avatar_decoration_url"))
                for r in rows
            ],
        )
        await conn.commit()
        print(f"💾 [MEMBERS] Saved {len(rows)} members")
        return True
    except Exception as e:
        print(f"❌ Failed to save members.db for guild {guild_id}: {e}")
        return False
    finally:
        await conn.close()


async def save_guild_db(guild_data: dict):
    """
    🌸 Persist a guild_data dict into FOUR separated databases:
    - metadata.db: guild_info
    - roles.db: roles + members + member_roles
    - channels.db: channels
    - members.db: id / global_name / username / server_name / avatar_url /
      banner_url per member (built from guild_data["members"] — see the
      extra keys groq_service.sync_guild_to_db puts on each member dict)
    
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
    
    # ══════════════════════════════════════════════════════════════════
    # 4️⃣ MEMBERS DATABASE (members.db) — account identity + CDN links
    # ══════════════════════════════════════════════════════════════════
    # Own failure domain: save_members_db never raises (it logs and returns
    # False), so a problem here can't undo the three saves above.
    await save_members_db(
        guild_id,
        [
            {
                "id": m.get("id"),
                "global_name": m.get("global_name"),
                "username": m.get("username"),
                "server_name": m.get("server_name") or m.get("display_name"),
                "avatar_url": m.get("avatar_url"),
                "banner_url": m.get("banner_url"),
                "accent_color": m.get("accent_color"),
                "avatar_decoration_url": m.get("avatar_decoration_url"),
            }
            for m in guild_data.get("members", [])
        ],
        expected_total=guild_data.get("member_count"),
    )

    print(f"✅ Guild {guild_data.get('name')} ({guild_id}) saved to all 4 databases\n")


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


async def get_members_with_role(guild_id: int, role_name: str, role_id: int | None = None) -> list[dict]:
    """
    🌸 Query roles.db to find all members who have a specific role.
    Returns a list of member dicts with id, username, display_name, roles.

    🌸 `role_id` (new, optional): look the role up by its snowflake instead
    of by name — used when someone asks with a role ID or a <@&ID> mention
    (server_queries.handle_role_query). Skips the name lookup entirely, so
    two roles that share a name can't be confused. `role_name` is ignored
    when role_id is given.
    
    Usage:
        members = await get_members_with_role(1479158469981376656, "Senior Moderator")
        for m in members:
            print(f"  {m['display_name']} (@{m['username']})")
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return []

    conn = await _get_roles_db(guild_id)
    try:
        if role_id is None:
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


async def find_member_by_id(guild_id: int, member_id: int) -> list[dict]:
    """
    🌸 Exact snowflake lookup in the guild's roles.db `members` table —
    the ID counterpart to find_member_by_name, for "is 123456789012345678
    here" / "<@123456789012345678>" style asks. Returns a list of 0 or 1
    member dicts in the SAME shape find_member_by_name returns (id,
    username, display_name, discriminator, avatar, bot), so callers can
    treat both interchangeably. [] = that ID isn't in the cached member
    table (never joined, left, or cache not populated yet).
    """
    if not member_id or not os.path.exists(_guild_db_dir(guild_id)):
        return []

    conn = await _get_roles_db(guild_id)
    try:
        cur = await conn.execute(
            "SELECT id, username, display_name, discriminator, avatar, bot FROM members WHERE id = ?",
            (int(member_id),),
        )
        row = await cur.fetchone()
        if not row:
            return []
        return [{
            "id": row[0],
            "username": row[1],
            "display_name": row[2],
            "discriminator": row[3],
            "avatar": row[4],
            "bot": bool(row[5]),
        }]
    except Exception as e:
        print(f"❌ Query error (find_member_by_id): {e}")
        return []
    finally:
        await conn.close()


async def find_member_by_name(guild_id: int, name: str) -> list[dict]:
    """
    🌸 Look up cached members by username/display_name (case-insensitive)
    in the guild's roles.db `members` table — the same table
    get_members_with_role already reads member rows from. Added for
    "does USER have role X" lookups (handle_user_role_query in
    groq_instruct.py) where the message gives a plain-text name instead
    of an @mention, so there's no member_id to go on yet.

    Tries an exact (case-insensitive) match on username OR display_name
    first; if nothing exact hits, falls back to a substring match on
    either field. Returns a list of member dicts (id, username,
    display_name, discriminator, avatar, bot) — callers should treat
    anything but exactly 1 result as "can't confidently resolve" (0 =
    not found, 2+ = ambiguous), same convention as
    handle_role_query's own partial-role-match handling.

    If all of those miss (typos like "Spy Ultime" for "Spy Ultimate"), a
    final fuzzy stage scores every member with difflib (threshold 0.60) and
    returns only the best match(es).

    Usage:
        matches = await find_member_by_name(1479158469981376656, "its.cuteee_")
        if len(matches) == 1:
            roles = await get_member_roles(guild_id, matches[0]["id"])
    """
    if not name or not os.path.exists(_guild_db_dir(guild_id)):
        return []

    conn = await _get_roles_db(guild_id)
    # 🌸 Rows readable by column label (r["username"]) AND by index (r[0]),
    # so every stage below can build the result dicts by key.
    conn.row_factory = aiosqlite.Row
    try:
        cur = await conn.execute(
            """
            SELECT id, username, display_name, discriminator, avatar, bot
            FROM members
            WHERE LOWER(username) = LOWER(?) OR LOWER(display_name) = LOWER(?)
            """,
            (name, name),
        )
        rows = await cur.fetchall()

        if not rows:
            # 🌸 BUGFIX: SQLite's LIKE treats bare "_" as a single-char
            # wildcard and "%" as a multi-char wildcard — Discord usernames
            # commonly contain literal underscores (e.g. "its.cuteee_"),
            # which were silently being read as "any one character" here
            # instead of a literal underscore. That could make this
            # fallback miss the real member (if the un-escaped pattern's
            # implicit wildcard didn't line up with any row) or return
            # 2+ ambiguous matches (if it accidentally matched OTHER
            # members too) — either way the caller (handle_user_role_query)
            # sees "not exactly 1 match" and fails open, which is exactly
            # the silently-wrong "does its.cuteee_ have role p" case.
            # Escaping the user-supplied name's own "_"/"%" chars before
            # wrapping it in wildcards makes only the wrapping % symbols
            # act as wildcards, restoring literal matching for the name.
            escaped_name = name.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
            like = f"%{escaped_name}%"
            cur = await conn.execute(
                """
                SELECT id, username, display_name, discriminator, avatar, bot
                FROM members
                WHERE username LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR display_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                """,
                (like, like),
            )
            rows = await cur.fetchall()

        if not rows:
            # 🌸 3rd stage: ignore punctuation / spaces / emoji decorations.
            # Same 6 columns in the same order, so r[0]..r[5] below still line up.
            rows = await _normalized_member_rows(
                conn,
                "id, username, display_name, discriminator, avatar, bot",
                ("username", "display_name"),
                name,
            )

        if not rows:
            # 🌸 4th stage: typo safety net (fuzzy match, threshold 0.60).
            cur = await conn.execute("SELECT * FROM members")
            all_rows = await cur.fetchall()
            rows = _fuzzy_pick(all_rows, ("username", "display_name"), name)

        return [
            {
                "id": r["id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "discriminator": r["discriminator"],
                "avatar": r["avatar"],
                "bot": bool(r["bot"]),
            }
            for r in rows
        ]
    except Exception as e:
        print(f"❌ Query error (find_member_by_name): {e}")
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


async def get_staff_context_string(guild_id: int) -> str:
    """
    🌸 STAFF INTENT INJECTOR — cheap local-DB fetch for the AI-intent-
    classifier's QUERY_STAFF fast path (see groq_ai.classify_user_intent
    + the interceptor wired at the top of get_ai_response). Meant to be
    called INSTEAD OF a full Groq chat generation whenever the message
    is asking "who's staff / who are the mods" — same shape as
    get_compact_roles/get_member_roles above, just pre-filtered to the
    two staff-flagged role names and pre-formatted as a display string
    so the caller can reply with it directly, no further LLM pass
    needed.

    Opens roles.db with the same WAL + NORMAL synchronous pragmas as
    _get_roles_db (same schema) rather than calling that helper
    directly, since this is a standalone, deliberately-minimal
    connection for a hot, latency-sensitive interceptor path — it only
    ever reads, never touches CREATE TABLE, so it doesn't need the rest
    of _get_roles_db's setup.

    Returns "" (empty string) if the guild has no cached roles.db yet,
    or if the query errors/finds nobody — callers should treat an empty
    return as "nothing to show" and fall back to the normal chat path
    rather than reply with a blank message.

    Usage:
        staff_text = await get_staff_context_string(guild_id)
        if staff_text:
            await message.reply(staff_text)
    """
    if not os.path.exists(_guild_db_dir(guild_id)):
        return ""

    conn = await aiosqlite.connect(_guild_roles_db_path(guild_id))
    try:
        # 🌸 Same WAL/synchronous pragmas as every other guild DB
        # connection in this file (see _get_roles_db, _get_metadata_db,
        # _get_groq_db) — WAL lets this read-only interceptor query run
        # concurrently with any in-flight writer (e.g. a guild-sync task
        # repopulating member_roles) without blocking on a file lock,
        # and NORMAL trades a sliver of durability for lower fsync
        # overhead, which is the right tradeoff for a cache DB that's
        # fully rebuildable from Discord's own API anyway.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")

        cur = await conn.execute("""
            SELECT m.display_name, r.name
            FROM member_roles mr
            JOIN members m ON m.id = mr.member_id
            JOIN roles r ON r.id = mr.role_id
            WHERE r.name IN ('Senior Moderator', 'Moderator')
            ORDER BY r.position DESC, m.display_name ASC
        """)
        rows = await cur.fetchall()

        if not rows:
            return ""

        lines = [f"- {display_name} ({role_name})" for display_name, role_name in rows]
        return "\n".join(lines)
    except Exception as e:
        print(f"❌ Query error (get_staff_context_string): {e}")
        return ""
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
