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
from config_loader import get_owner_id, get_test_guild_id, get_log_channel_ids

# 🌸 Live stdout/stderr → Discord webhook (color-coded success/warning/error
# embeds), set in discord_config.py → WEBHOOKS["log_webhook_url"]. Started
# as early as possible so even startup prints/tracebacks get captured.
from log_webhook import start_log_webhook
start_log_webhook()

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
# Path is relative to CWD (bot root), matching this file's existing convention
# of using relative paths like "gemini/configuration/gen_ai" instead of __file__-based ones.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

# 🌸 Gemini logic now lives in its own module — see gemini_service.py
from gemini_service import GeminiService

# Import separated modules
from groq_ai import GroqService
from groq_service import GroqMentionService

# 🌸 DM-trigger gate now lives in extras/groq_dm.py — see is_dm_trigger.
from extras.groq_dm import is_dm_trigger
from groq_music_suggestion import register_persistent_music_view
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

        # 🌸 Mention/reply handling + server-question interceptors + the
        # guild-data sync/cache that feeds them — see groq_service.py.
        self.groq_mentions = GroqMentionService(bot=self)

        # 🌸 REFACTORED: Groq activity/error log channels now live in
        # discord_config.py → BOT["log_channel_ids"]. Supports any number
        # of channels; every configured channel gets BOTH the 429
        # rate-limit alerts and the per-message success logs (see
        # _broadcast_log_embed).
        self.log_channel_ids = get_log_channel_ids()
        if not self.log_channel_ids:
            print("⚠️ WARNING: log_channel_ids not set in discord_config.py!")

        # 🌸 channel_id -> unix timestamp of when Groq priority-reply is
        # allowed to fire again. Empty/expired = Groq is off cooldown.
        self.groq_cooldowns = {}

        self.download_dir = "downloads"
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

        # 🌐 Network watchdog state — see network_watchdog() below
        self._net_online = True
        self._net_offline_backoff = NETWORK_CHECK_OFFLINE_MIN

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
        # 🌸 extras/groq_dm.is_dm_trigger — separate module so the DM
        # eligibility rule (currently just "is this a DMChannel") has
        # room to grow without touching on_message itself.
        is_dm_reply = is_dm_trigger(message, self.user)

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
        # (single shared typing pulse for the whole flow). Delegated to
        # groq_service.GroqMentionService — see self.groq_mentions.
        if is_dm_reply or is_mentioned or await self.groq_mentions.is_reply_to_bot(message):
            await self.groq_mentions.handle_mention_reaction(message)

        await self.process_commands(message)

    async def on_ready(self):
        """🌸 Fires once the gateway handshake is complete and self.guilds
        is actually populated — this is the earliest point a full guild
        data sync is possible. Runs in the background (not awaited here)
        so a big/slow guild chunking doesn't hold up anything else."""
        print(f"🌸 Logged in as {self.user} — {len(self.guilds)} guild(s) connected.")

        # 🌐 on_ready only fires on a fresh IDENTIFY (never on a RESUME).
        # A short blip resumes the old session and keeps the presence
        # bubble intact — but ~10+ minutes offline invalidates the
        # session, forcing a full IDENTIFY that wipes presence back to
        # default. sync_loop's dedupe (state_key == self.last_state)
        # would otherwise skip resending since status.json never
        # changed, so force it to resync on its next 1s tick.
        self.last_state = None

        # 🌸 Guild data sync now lives on groq_mentions (GroqMentionService)
        # — see groq_service.py.
        if not self.groq_mentions._initial_guild_sync_done:
            self.groq_mentions._initial_guild_sync_done = True
            asyncio.create_task(self.groq_mentions.sync_all_guilds())

    async def on_guild_join(self, guild: discord.Guild):
        """🌸 Sync a newly-joined guild's data immediately instead of
        waiting for the bot to restart before metadata.db/roles.db/
        channels.db know it exists."""
        print(f"➕ Joined new guild: {guild.name} ({guild.id}) — syncing...")
        await self.groq_mentions.sync_guild_to_db(guild)

    async def setup_hook(self):
        """🌸 Setup hook — runs once on startup before connecting."""
        connector    = aiohttp.TCPConnector(limit=10, force_close=True, enable_cleanup_closed=True)
        self.session = aiohttp.ClientSession(connector=connector)

        # 🌸 Persistent view registration MUST happen here (before the
        # gateway connects), not in on_ready — this is what makes the
        # music paginator's ◀️/▶️ buttons keep working on OLD messages
        # after a bot restart. See MusicPaginatorView / custom_id
        # scheme in groq_music_suggestion.py.
        register_persistent_music_view(self)

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
