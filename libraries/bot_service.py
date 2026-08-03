import discord
import json
import os
import sys
import aiohttp
import asyncio
import argparse
import time
import random
import re
from datetime import datetime
from discord.ext import tasks, commands
from discord import app_commands
from collections import deque

# 🌸 NEW: Config loader for Discord snowflake IDs
from config_loader import get_owner_id, get_test_guild_id

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
# Path is relative to CWD (bot root), matching this file's existing convention
# of using relative paths like "gemini/configuration/gen_ai" instead of __file__-based ones.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

# 🌸 Gemini logic now lives in its own module — see gemini_service.py
from gemini_service import GeminiService

# Import separated modules
from groq_instruct import (
    handle_role_query, handle_created_query, handle_server_info_query,
    handle_channel_count_query, handle_role_list_query,
    handle_server_avatar_query, handle_server_banner_query,
    handle_server_owner_query, handle_server_verification_query,
    handle_member_count_query, handle_server_age_query,
    handle_boost_status_query, handle_locale_query,
    CONTEXT_TRIGGERS, KEYWORD_STOPWORDS,
    REACT_TAG_PATTERN, REACT_REQUEST_PATTERN, EXPLICIT_EMOJI_PATTERN,
    REACT_EMOJI_POOL, RECENT_EMOJI_MEMORY, AUTO_REACT_CHANCE,
    _build_react_instructions, EMOJI_NAME_MAP
)
from groq_ai import GroqService
from roulette import RouletteService
from discord_commands import register_all_cogs
from resources import shared

# ─────────────────────────────────────────────────────────────────────────────
# When bot_service.py is run directly Python only adds *this* directory
# (libraries/) to sys.path — it has no idea about the parent package.
# Insert the parent (bot/) so `from libraries import ...` resolves correctly.
# ─────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─────────────────────────────────────────────────────────────────────────────
# Custom modules — all declared in libraries/__init__.py
# ─────────────────────────────────────────────────────────────────────────────
from libraries import *

# ─────────────────────────────────────────────────────────────────────────────
# 🌐 Network watchdog config — an interface change (Termux wifi↔mobile-data
# handoff, Windows sleep/wake, a Linux box's ethernet flapping, VPN toggles,
# etc.) can leave aiohttp's TCPConnector holding dead sockets without ever
# raising a clean error — they just hang. This probes real connectivity with
# plain asyncio TCP (no OS-specific APIs, works the same on Android/Termux,
# Windows, and Linux/Ubuntu) independently of discord.py/aiohttp, and
# recycles the session once it's back.
# ─────────────────────────────────────────────────────────────────────────────
NETWORK_PROBE_HOSTS = [("1.1.1.1", 443), ("8.8.8.8", 443), ("discord.com", 443)]
NETWORK_CHECK_ONLINE_INTERVAL = 30   # seconds between checks while healthy
NETWORK_CHECK_OFFLINE_MIN = 5        # first retry after a drop
NETWORK_CHECK_OFFLINE_MAX = 30       # backoff cap while still offline

# 🌸 Same IS_TERMUX detection pattern already used elsewhere (ffmpeg
# mediacodec flag, aria2c check) — only used here to word log messages
# accurately per platform, doesn't change any actual probing logic.
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "")
NET_LABEL = "wifi/data" if IS_TERMUX else "network"

# 🔔 Reply-ping policy — pings ONLY the person being replied to (the
# trigger). @everyone/@here and role mentions never notify anyone even if
# they somehow end up in the reply text, and neither do any OTHER user
# mentions the AI-generated text might contain (accidental @-ing a
# bystander). Shared by every message.reply() call in handle_mention_reaction.
SAFE_REPLY_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=True)


class EnchantedBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(command_prefix="Ct~!", *args, **kwargs)

        self.base_path   = os.path.dirname(os.path.abspath(__file__))
        self.status_dir  = os.path.join(self.base_path, "status")
        self.status_file = os.path.join(self.status_dir, "status.json")

        os.makedirs(self.status_dir, exist_ok=True)

        # 🌸 REFACTORED: owner_id now lives in discord_config.json
        self.owner_id = get_owner_id()
        if not self.owner_id:
            print("⚠️ WARNING: owner_id not set in discord_config.json!")
        self.last_state  = None
        self.session     = None
        self.ai          = GeminiService(bot=self)
        self.groq        = GroqService(bot=self)
        self.roulette    = RouletteService()

        # 🌸 Groq activity/error log channels — auth/log_channels.txt, one
        # Discord channel snowflake ID per line. Supports any number of
        # channels; every configured channel gets BOTH the 429 rate-limit
        # alerts and the per-message success logs (see _broadcast_log_embed).
        self.log_channel_ids = self._load_log_channel_ids("auth/log_channels.txt")

        # 🌸 channel_id -> unix timestamp of when Groq priority-reply is
        # allowed to fire again. Empty/expired = Groq is off cooldown.
        self.groq_cooldowns = {}

        # 🌸 channel_id -> deque of the last RECENT_EMOJI_MEMORY emoji this
        # channel auto-reacted with (both Groq-picked and directly-requested
        # ones — see _record_reaction_emoji). Fed back into get_ai_response
        # as an "avoid these" list so the bot stops defaulting to the same
        # emoji (e.g. 🤗) over and over.
        self.recent_react_emoji = {}

        # 🌸 guild.id -> (unix timestamp fetched, stripped server-info dict)
        # — see get_server_context_text / GUILD_INFO_CACHE_TTL. Keeps Groq
        # from triggering a v10 REST call to /guilds/{id} on every single
        # mention in a server.
        self.guild_info_cache = {}
        
        # 🌸 guild.id -> full guild data (v10 JSON format with channels,
        # roles, members, etc.). Loaded from cache/guild_data/ on startup.
        # Provides complete server context to Groq AI.
        self.guild_cache = {}

        # 🌸 Guards sync_all_guilds() so it only runs once per process —
        # on_ready can fire more than once (e.g. after a gateway reconnect)
        # and we don't want to re-chunk + re-save every guild each time.
        self._initial_guild_sync_done = False

        self.download_dir = "downloads"
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

        # 🌐 Network watchdog state — see network_watchdog() below
        self._net_online = True
        self._net_offline_backoff = NETWORK_CHECK_OFFLINE_MIN

    def _load_log_channel_ids(self, path: str) -> list[int]:
        """🌸 Reads one Discord channel snowflake ID per line from `path`.
        Blank lines and lines starting with '#' are skipped so the file can
        be commented. Bad/non-numeric lines are warned about and skipped
        rather than crashing startup."""
        ids = []
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.isdigit():
                        ids.append(int(line))
                    else:
                        print(f"⚠️ log_channels.txt: skipping invalid line {line!r}")
        except FileNotFoundError:
            print(f"⚠️ {path} not found — Groq logging embeds are disabled until it's created.")
        except Exception as e:
            print(f"⚠️ Failed to load log channels: {e}")
        return ids

    async def _broadcast_log_embed(self, embed: discord.Embed):
        """🌸 Sends `embed` to every channel in self.log_channel_ids.
        Best-effort per channel — one missing/forbidden channel never stops
        the loop. Re-raises on critical errors (e.g. HTTP 401/403 from Discord
        auth), but skips individual 404s silently."""
        if not self.log_channel_ids:
            return

        for channel_id in self.log_channel_ids:
            try:
                channel = self.get_channel(channel_id)
                if not channel:
                    continue
                await channel.send(embed=embed)
            except discord.Forbidden:
                print(f"⚠️ log channel {channel_id}: 403 Forbidden (is bot member + has send perms?)")
            except discord.HTTPException as e:
                if e.status == 404:
                    continue
                raise
            except Exception as e:
                print(f"⚠️ log channel {channel_id}: {e}")

    async def sync_guild_to_db(self, guild: discord.Guild):
        """🌸 Builds a COMPLETE guild_data dict straight from the live
        discord.py Guild object and persists it into metadata.db/roles.db/
        channels.db via shared.save_guild_db(). No extra REST calls needed:
        - guild.created_at is decoded locally from the snowflake ID
        - guild.roles / guild.channels / guild.members are already
          populated by the gateway (Intents.all() covers members too, as
          long as the "Server Members Intent" toggle is also on in the
          Discord Dev Portal — if it's off, guild.members will just be
          the bot itself and member_count will still be accurate).

        Previously `load_guild_caches()` was a stub that only ever wrote
        {"id": ..., "cached_at": ...} into memory and never touched the
        SQLite files at all — that's why fields like created_at, owner_id,
        and member_count sat as NULL in metadata.db forever, and why
        handle_created_query() had nothing to read (see July 2026 "idk the
        exact date" bug).
        """
        try:
            # Large/lazy guilds may not have their full member list synced
            # yet — chunk() blocks until the gateway sends every member.
            if not guild.chunked and self.intents.members:
                try:
                    await guild.chunk(cache=True)
                except Exception as e:
                    print(f"⚠️ Chunking failed for {guild.name} ({guild.id}): {e}")

            guild_data = {
                "id": guild.id,
                "name": guild.name,
                "owner_id": guild.owner_id,
                "created_at": guild.created_at.isoformat() if guild.created_at else None,
                "member_count": guild.member_count,
                "description": guild.description,
                "icon": guild.icon.key if guild.icon else None,
                "banner": guild.banner.key if guild.banner else None,
                "preferred_locale": str(guild.preferred_locale) if guild.preferred_locale else None,
                "verification_level": str(guild.verification_level),
                "default_notifications": str(guild.default_notifications),
                "explicit_content_filter": str(guild.explicit_content_filter),
                "mfa_level": str(guild.mfa_level),
                "nsfw_level": str(guild.nsfw_level),
                "roles": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "color": r.color.value,
                        "hoist": r.hoist,
                        "position": r.position,
                        "permissions": str(r.permissions.value),
                        "managed": r.managed,
                        "mentionable": r.mentionable,
                        "icon": r.icon.key if r.icon else None,
                    }
                    for r in guild.roles
                ],
                "channels": [
                    {
                        "id": ch.id,
                        "name": ch.name,
                        "type": str(ch.type),
                        "position": getattr(ch, "position", 0) or 0,
                        "parent_id": ch.category_id if hasattr(ch, "category_id") else None,
                        "nsfw": getattr(ch, "nsfw", False),
                        "topic": getattr(ch, "topic", None),
                        "slowmode_delay": getattr(ch, "slowmode_delay", None),
                        "default_auto_archive_duration": getattr(ch, "default_auto_archive_duration", None),
                        "bitrate": getattr(ch, "bitrate", None),
                        "user_limit": getattr(ch, "user_limit", None),
                    }
                    for ch in guild.channels
                ],
                "members": [
                    {
                        "id": m.id,
                        "username": m.name,
                        "display_name": m.display_name,
                        "discriminator": m.discriminator,
                        "avatar": m.avatar.key if m.avatar else None,
                        "bot": m.bot,
                        "system": m.system,
                        "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                        "nickname": m.nick,
                        "pending": bool(getattr(m, "pending", False)),
                        "roles": [r.id for r in m.roles if r.name != "@everyone"],
                    }
                    for m in guild.members
                ],
            }

            await shared.save_guild_db(guild_data)

            # Lightweight in-memory marker (kept for parity with the old
            # behavior — the real source of truth is now the SQLite files).
            self.guild_cache[guild.id] = {
                "id": guild.id,
                "cached_at": datetime.now().isoformat()
            }
        except Exception as e:
            print(f"⚠️ Failed to sync guild {guild.name} ({guild.id}) to DB: {e}")

    async def sync_all_guilds(self):
        """🌸 Fetches + saves FULL data (metadata, roles, channels, members)
        for every guild the bot is currently in. Called once from on_ready
        (guild data isn't reliably available yet during setup_hook, since
        that runs before the gateway finishes sending GUILD_CREATE events)
        so metadata.db/roles.db/channels.db are always fresh on boot,
        instead of relying on whatever partial data got written on-demand
        (e.g. by /server-info) — possibly months ago, possibly incomplete.
        """
        if not self.guilds:
            print("⚠️ sync_all_guilds: no guilds available yet.")
            return

        print(f"🔄 Syncing full guild data for {len(self.guilds)} guild(s)...")
        synced = 0
        for guild in self.guilds:
            await self.sync_guild_to_db(guild)
            synced += 1
        print(f"✅ Synced {synced}/{len(self.guilds)} guild(s) to metadata.db/roles.db/channels.db")

    def get_server_context_text(self, guild: discord.Guild) -> str:
        """🌸 Return a lightweight "where am I" identity line for Groq — just
        server name + member count, NOT the full roles/channels dump. The
        detailed compact summary (top roles, channels) is pulled from the
        SQLite cache (metadata.db/roles.db/channels.db) via
        shared.get_guild_context_summary inside groq_ai.get_ai_response
        instead, so it's DB-backed and token-efficient rather than a live
        guild.roles/guild.channels dump that grows unbounded on big servers.
        """
        GUILD_INFO_CACHE_TTL = 300  # 5 minutes

        cached = self.guild_info_cache.get(guild.id)
        now = time.time()

        if cached and (now - cached.get("fetched_at", 0)) < GUILD_INFO_CACHE_TTL:
            return cached["text"]

        context_text = f"📍 You are currently in **{guild.name}** ({guild.member_count or '?'} members)."

        self.guild_info_cache[guild.id] = {
            "text": context_text,
            "fetched_at": now
        }
        return context_text

    async def is_reply_to_bot(self, message: discord.Message) -> bool:
        """🌸 True if `message` is a reply to one of the bot's messages."""
        if not message.reference:
            return False

        try:
            replied_to = await message.channel.fetch_message(message.reference.message_id)
            return replied_to.author.id == self.user.id
        except Exception:
            return False

    async def handle_mention_reaction(self, message: discord.Message):
        """🌸 Mention / reply reaction handler (e.g. random message roulette).
        Combines message content + Groq AI response + emoji reaction.
        """
        async with message.channel.typing():
            guild = message.guild

            # 🌸 REGEX INTERCEPTORS — cheap, instant, token-free answers for
            # specific question shapes, pulled straight from the SQLite
            # guild cache instead of ever hitting Groq. Falls through to
            # Groq normally if none match (or no cache yet).
            if guild:
                # 🌸 Most specific/rare patterns checked first (avatar, banner,
                # owner, verification, boosts) so they never get shadowed by the more
                # general "what is this server" catch-all below.
                intercepted = await handle_server_avatar_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_server_banner_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_server_owner_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_server_verification_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_member_count_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_server_age_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_boost_status_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_locale_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_server_info_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_channel_count_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_role_list_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_role_query(message, guild.id, shared)
                if intercepted is None:
                    intercepted = await handle_created_query(message, guild.id, shared)
                if intercepted:
                    await message.reply(intercepted, mention_author=True, allowed_mentions=SAFE_REPLY_MENTIONS)
                    return

            # 🌸 Prompt no longer carries the full guild_context dump — the
            # compact, DB-backed guild summary is now injected inside
            # groq_ai.get_ai_response via shared.get_guild_context_summary,
            # so this prompt stays just the user's actual message.
            prompt = (
                f"User: {message.author.name}\n"
                f"Message: {message.content}\n"
            )

            # ✅ Check if user is asking for a reaction
            user_asking_for_react = bool(REACT_REQUEST_PATTERN.search(message.content))
            
            # ✅ Determine which emoji to suggest (if allowed to react)
            react_allowed = user_asking_for_react or random.random() < AUTO_REACT_CHANCE

            response = await self.groq.get_ai_response(
                prompt,
                username=message.author.name,
                user_id=message.author.id,
                display_name=message.author.display_name,
                guild=message.guild,
                channel=message.channel,
                recent_react_emoji=self.recent_react_emoji.get(str(message.channel.id), []),
                react_allowed=react_allowed,  # ✅ ADDED: Pass react_allowed
                shared=shared
            )

            if response:
                # ✅ Extract and apply emoji reactions BEFORE stripping tags
                await self._apply_emoji_reactions(message, response)
                
                # ✅ Strip [REACT:emoji] tags from response text so they don't show to users
                clean_response = REACT_TAG_PATTERN.sub("", response).strip()
                
                try:
                    if clean_response:  # Only send if there's content after stripping
                        reply = await message.reply(
                            content=clean_response[:2000],
                            mention_author=True,
                            allowed_mentions=SAFE_REPLY_MENTIONS,
                            suppress_embeds=False
                        )
                except discord.HTTPException as e:
                    print(f"⚠️ Failed to reply: {e}")

    async def _apply_emoji_reactions(self, message: discord.Message, response: str):
        """🌸 Extract [REACT:emoji] tags from response and apply them to the original message."""
        try:
            # Extract emoji from [REACT:emoji] tags
            match = REACT_TAG_PATTERN.search(response)
            if match:
                emoji_str = match.group(1).strip()
                
                # Try to react with the emoji
                try:
                    await message.add_reaction(emoji_str)
                    
                    # ✅ Record this emoji in recent_react_emoji for this channel
                    channel_id = str(message.channel.id)
                    if channel_id not in self.recent_react_emoji:
                        self.recent_react_emoji[channel_id] = []
                    
                    # Keep only last N emoji to avoid repeats
                    self.recent_react_emoji[channel_id].append(emoji_str)
                    if len(self.recent_react_emoji[channel_id]) > RECENT_EMOJI_MEMORY:
                        self.recent_react_emoji[channel_id].pop(0)
                    
                    print(f"✅ Reacted to {message.author.name}'s message with {emoji_str}")
                except discord.HTTPException as e:
                    print(f"⚠️ Failed to add reaction {emoji_str}: {e}")
        except Exception as e:
            print(f"⚠️ Error extracting emoji reactions: {e}")

    async def reload_all_modules(self):
        """🌸 Dynamically reload all custom modules (for development/debugging).
        Delegates to reload_manager.py so the module list / cog re-registration
        logic lives in its own file instead of bloating bot_service.py.
        The /reload slash command (system_commands.py) calls this too."""
        from reload_manager import reload_all
        return await reload_all(self)

    @tasks.loop(seconds=1)
    async def sync_loop(self):
        """🌸 Periodic sync of bot presence/activity from status.json"""
        try:
            # Wait for bot to be ready before attempting to change presence
            if not self.is_ready():
                return
            
            if not os.path.exists(self.status_file):
                return

            with open(self.status_file, "r") as f:
                status_data = json.load(f)

                bubble    = status_data.get("bubble", "")
                act_type  = status_data.get("type", "custom")
                act_name  = status_data.get("name", "")
                emoji_str = status_data.get("emoji", "")
                stream_url = status_data.get("url", "https://twitch.tv/discord")

                state_key = f"{bubble}|{act_type}|{act_name}|{emoji_str}|{stream_url}"

                if state_key != self.last_state:
                    activity = None

                    parsed_emoji = None
                    if emoji_str:
                        try:
                            parsed_emoji = discord.PartialEmoji.from_str(emoji_str)
                        except Exception:
                            parsed_emoji = None

                    if act_type == "custom" and bubble:
                        activity = discord.CustomActivity(name=bubble, emoji=parsed_emoji)
                    elif act_type == "watching" and act_name:
                        activity = discord.Activity(type=discord.ActivityType.watching, name=act_name)
                    elif act_type == "listening" and act_name:
                        activity = discord.Activity(type=discord.ActivityType.listening, name=act_name)
                    elif act_type == "streaming" and act_name:
                        activity = discord.Streaming(name=act_name, url=stream_url)
                    elif act_type == "playing" and act_name:
                        activity = discord.Game(name=act_name)
                    elif bubble:
                        activity = discord.CustomActivity(name=bubble, emoji=parsed_emoji)

                    await self.change_presence(activity=activity, status=discord.Status.online)
                    self.last_state = state_key
        except Exception as e:
            print(f"⚠️ Sync Loop Error: {e}")

    async def _probe_internet(self) -> bool:
        """🌐 Raw TCP probe, deliberately NOT using self.session — a wedged
        aiohttp connector (stale sockets after an interface change, e.g.
        Termux wifi↔data, Windows sleep/wake, Linux link flap) would
        otherwise report 'online' even while every real request just hangs.
        Tries each host in NETWORK_PROBE_HOSTS until one connects. Uses
        plain asyncio, so this works identically on every platform."""
        for host, port in NETWORK_PROBE_HOSTS:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=4
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return True
            except Exception:
                continue
        return False

    async def _recover_after_reconnect(self):
        """🌐 Runs once, right when connectivity flips offline → online.
        Recycles self.session's connector (old sockets can be silently
        dead after the interface switch, on any OS) so Groq/Gemini/media
        calls stop hanging on connections that look alive but aren't."""
        try:
            old_session = self.session
            connector = aiohttp.TCPConnector(limit=10, force_close=True, enable_cleanup_closed=True)
            self.session = aiohttp.ClientSession(connector=connector)
            if old_session and not old_session.closed:
                await old_session.close()
            print("🌐🔄 aiohttp session recycled after reconnect.")
        except Exception as e:
            print(f"⚠️ Failed to recycle session after reconnect: {e}")

        # discord.py's own gateway (bot.run(..., reconnect=True)) already
        # retries the websocket with its own backoff — this just reports
        # whether it's caught back up yet, no manual relogin needed.
        if self.is_closed():
            print("⚠️ Discord gateway still closed — waiting on discord.py's own reconnect loop.")
        elif not self.is_ready():
            print("⏳ Discord gateway not ready yet post-reconnect — waiting...")
        else:
            print("✅ Discord gateway confirmed alive post-reconnect.")

    @tasks.loop(seconds=NETWORK_CHECK_OFFLINE_MIN)
    async def network_watchdog(self):
        """🌐 Detects wifi/data drops and retries until connectivity comes
        back, then triggers recovery. Checks fast (backing off up to
        NETWORK_CHECK_OFFLINE_MAX) while offline, and relaxes to
        NETWORK_CHECK_ONLINE_INTERVAL once healthy again."""
        try:
            online = await self._probe_internet()

            if online:
                if not self._net_online:
                    print(f"🌐✅ Internet back ({NET_LABEL} reconnected) — recovering...")
                    await self._recover_after_reconnect()
                self._net_online = True
                self._net_offline_backoff = NETWORK_CHECK_OFFLINE_MIN
                self.network_watchdog.change_interval(seconds=NETWORK_CHECK_ONLINE_INTERVAL)
            else:
                if self._net_online:
                    print(f"🌐⚠️ Internet lost ({NET_LABEL} disconnected) — retrying...")
                self._net_online = False
                self._net_offline_backoff = min(
                    self._net_offline_backoff * 2, NETWORK_CHECK_OFFLINE_MAX
                )
                self.network_watchdog.change_interval(seconds=self._net_offline_backoff)
        except Exception as e:
            print(f"⚠️ Network watchdog error: {e}")

    @network_watchdog.before_loop
    async def before_network_watchdog(self):
        await self.wait_until_ready()

    async def log_to_inbox(self, message: discord.Message):
        """🌸 Log mentions and DMs to inbox for record-keeping"""
        try:
            # You can implement custom logging logic here
            pass
        except Exception as e:
            print(f"⚠️ Failed to log to inbox: {e}")

    async def download_attachments(self, message: discord.Message):
        """🌸 Download and store attachments from messages"""
        try:
            if not message.attachments:
                return
            
            for attachment in message.attachments:
                filepath = os.path.join(self.download_dir, attachment.filename)
                await attachment.save(filepath)
                print(f"📥 Downloaded: {attachment.filename}")
        except Exception as e:
            print(f"⚠️ Failed to download attachments: {e}")

    async def forward_dm_to_channel(self, message: discord.Message):
        """🌸 Forward DMs to owner's inbox channel"""
        try:
            # This forwards DMs to a specific channel
            # You can set the channel ID in your config
            inbox_channel_id = None  # Set your inbox channel ID here
            if not inbox_channel_id:
                return
            
            channel = self.get_channel(inbox_channel_id)
            if not channel:
                return
            
            embed = discord.Embed(
                title=f"📬 DM from {message.author}",
                description=message.content or "(no text)",
                color=0x3498db,
                timestamp=datetime.now()
            )
            embed.set_author(name=str(message.author), icon_url=message.author.avatar.url if message.author.avatar else None)
            
            if message.attachments:
                embed.add_field(
                    name="Attachments",
                    value="\n".join([a.filename for a in message.attachments]),
                    inline=False
                )
            
            await channel.send(embed=embed)
        except Exception as e:
            print(f"⚠️ Failed to forward DM: {e}")

    async def on_message(self, message):
        if message.author.bot:
            return

        is_dm       = isinstance(message.channel, discord.DMChannel)
        is_mentioned = self.user.mentioned_in(message)

        if is_dm or is_mentioned:
            await self.log_to_inbox(message)
            if message.attachments:
                await self.download_attachments(message)

        # 📬 Forward DMs to owner inbox channel
        if is_dm:
            await self.forward_dm_to_channel(message)

        # ✅ msg_cache now lives in shared.py
        cid = str(message.channel.id)
        if cid not in shared.msg_cache:
            shared.msg_cache[cid] = []
        shared.msg_cache[cid].insert(0, message)
        shared.msg_cache[cid] = shared.msg_cache[cid][:5]

        if message.author.id == self.owner_id:
            if message.content == "!sync":
                try:
                    await self.tree.sync()
                    await message.channel.send("🧹 **Bridge Re-Synced!**")
                except Exception as e:
                    await message.channel.send(f"❌ Sync failed: {e}")

            elif message.content == "!reload":
                # 🌸 DEPRECATED: Use /reload slash command instead
                try:
                    summary = await self.reload_all_modules()
                    await self.tree.sync()
                    n_ok  = len(summary["modules_reloaded"])
                    n_bad = len(summary["modules_failed"])
                    status = f"✅ **{n_ok} modules reloaded**"
                    if n_bad:
                        status += f" • ⚠️ {n_bad} failed: `{', '.join(summary['modules_failed'])}`"
                    status += "\n✅ **Commands synced!** 🌸✨\n\n💡 **Tip:** Use `/reload` slash command instead of `!reload` 💕"
                    await message.channel.send(status)
                    print(f"🔄 Full refresh completed via !reload by {message.author} at {datetime.now().strftime('%H:%M:%S')}")
                except Exception as e:
                    await message.channel.send(f"❌ **Reload/Sync Failed:** `{str(e)[:100]}` 🥺")

        # 🎲 Mention / reply-to-bot reaction — random message roulette
        # (single shared typing pulse for the whole flow)
        if is_mentioned or await self.is_reply_to_bot(message):
            await self.handle_mention_reaction(message)

        await self.process_commands(message)

    async def on_ready(self):
        """🌸 Fires once the gateway handshake is complete and self.guilds
        is actually populated — this is the earliest point a full guild
        data sync is possible. Runs in the background (not awaited here)
        so a big/slow guild chunking doesn't hold up anything else."""
        print(f"🌸 Logged in as {self.user} — {len(self.guilds)} guild(s) connected.")

        if not self._initial_guild_sync_done:
            self._initial_guild_sync_done = True
            asyncio.create_task(self.sync_all_guilds())

    async def on_guild_join(self, guild: discord.Guild):
        """🌸 Sync a newly-joined guild's data immediately instead of
        waiting for the bot to restart before metadata.db/roles.db/
        channels.db know it exists."""
        print(f"➕ Joined new guild: {guild.name} ({guild.id}) — syncing...")
        await self.sync_guild_to_db(guild)

    async def setup_hook(self):
        """🌸 Setup hook — runs once on startup before connecting."""
        connector    = aiohttp.TCPConnector(limit=10, force_close=True, enable_cleanup_closed=True)
        self.session = aiohttp.ClientSession(connector=connector)

        # 🌸 Full guild data sync (metadata/roles/channels/members) now
        # happens in on_ready instead — self.guilds is still empty here,
        # since setup_hook runs before the gateway sends GUILD_CREATE.

        # Register all cogs via the new discord_commands module
        await register_all_cogs(self)

        print("⏳ Syncing Bridge Commands...")
        
        # 🌸 REFACTORED: Test guild ID now lives in discord_config.json
        TEST_GUILD_ID = get_test_guild_id()
        if TEST_GUILD_ID:
            try:
                synced_guild = await self.tree.sync(guild=discord.Object(id=TEST_GUILD_ID))
                print(f"\n✅ Commands Synced to Test Guild (ID: {TEST_GUILD_ID})!")
                print(f"📊 Total: {len(synced_guild)} commands\n")
                for i, cmd in enumerate(synced_guild, 1):
                    print(f"  {i:2d}. 🌸 /{cmd.name:<30} (ID: {cmd.id})")
            except Exception as e:
                print(f"⚠️ Test guild sync failed: {e}")
        else:
            print("⚠️ No test_guild_id configured in discord_config.json")
        
        # 🌍 Global sync
        try:
            synced = await self.tree.sync()
            print(f"\n✅ Synced {len(synced)} commands globally!")
            print(f"📊 Command Summary:")
            for i, cmd in enumerate(synced, 1):
                print(f"  {i:2d}. /{cmd.name:<30} (ID: {cmd.id})")
        except Exception as e:
            print(f"⚠️ Global sync failed: {e}")
        
        self.sync_loop.start()
        self.network_watchdog.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token",
        required=False,
        help="Optional: path to a token file to override key_config.DISCORD_TOKEN.",
    )
    args = parser.parse_args()

    try:
        if args.token:
            with open(args.token, "r") as f:
                tkn = f.read().strip()
        else:
            tkn = key_config.DISCORD_TOKEN

        if not tkn:
            raise RuntimeError(
                "No Discord token found — set DISCORD_TOKEN in auth/key_config.py "
                "or pass --token pointing at a token file."
            )

        bot = EnchantedBot(intents=discord.Intents.all())
        bot.run(tkn, reconnect=True)
    except Exception as e:
        print(f"❌ Startup Error: {e}")
