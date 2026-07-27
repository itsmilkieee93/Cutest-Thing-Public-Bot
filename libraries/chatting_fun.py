"""
🎬 Live Stream Chat Fetcher
Uses pytchat  — no API key required.
Paste any YouTube live stream URL or 11-char video ID.
"""
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import aiohttp
import pytchat
import signal
import time
import logging
import random
import re
from datetime import datetime

# ── logger must be defined before anything that uses it ──
logger = logging.getLogger(__name__)

PASTEL_COLORS = [
    0xFFC0CB, 0xB57EDC, 0xFFD1DC, 0xAEC6CF, 0xB5EAD7,
    0xFFDAB9, 0xFFF0A0, 0xC9C0D3, 0xFFB7CE, 0xA8D8EA,
    0xFDFD96, 0xE0BBE4, 0x957DAD, 0xD4F0F0, 0xFFE5B4,
    0xE2F0CB, 0xFFCCF9, 0xC5E1A5, 0xF4978E, 0xB8E1FF,
]

LOADING_GIFS = [
    "https://c.tenor.com/knwWU-EgRmMAAAAC/tenor.gif",
    "https://c.tenor.com/J9mOaXMbKygAAAAC/tenor.gif",
    "https://c.tenor.com/plvrL3peoBIAAAAC/tenor.gif",
    "https://c.tenor.com/Yo4Vo-XCgqEAAAAC/tenor.gif",
    "https://c.tenor.com/ts-81PaXp3AAAAAC/tenor.gif",
    "https://c.tenor.com/Ly_w3cT7B04AAAAC/tenor.gif",
]


def extract_video_id(query: str) -> str | None:
    """Extract a YouTube video ID from any known URL format, or return None."""
    if len(query) == 11 and " " not in query:
        return query
    if "youtube.com/watch?v=" in query:
        return query.split("v=")[1].split("&")[0]
    if "youtu.be/" in query:
        return query.split("youtu.be/")[1].split("?")[0]
    if "youtube.com/live/" in query:
        return query.split("youtube.com/live/")[1].split("?")[0]
    if "youtube.com/shorts/" in query:
        return query.split("youtube.com/shorts/")[1].split("?")[0]
    if "youtube.com/embed/" in query:
        return query.split("youtube.com/embed/")[1].split("?")[0]
    return None


async def resolve_url(url: str) -> str:
    """Follow redirects (handles pastelink/shorteners) and return the final URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return str(resp.url)
    except Exception:
        return url


def _display_name(raw: str) -> str:
    """Return the channel display name, stripping the leading @ handle prefix
    that YouTube now returns in authorName for some channels."""
    if not raw:
        return ""
    return raw.lstrip("@") if raw.startswith("@") else raw


def _hires_avatar(url: str, size: int = 0) -> str:
    """
    Upsize a pytchat author avatar URL.

    NOTE: channel avatars are NOT served as named files like video
    thumbnails are (default.jpg / hqdefault.jpg / maxresdefault.jpg).
    They're served from yt3.ggpht.com / lh3.googleusercontent.com with a
    resolution baked into the query-ish path, e.g.:
        https://yt3.ggpht.com/xyz=s64-c-k-c0x00ffffff-no-rj
    Swapping that "s64" for a bigger number (or "s0" for the original,
    unscaled upload) gets you a much bigger avatar image. size=0 = original.
    """
    if not url:
        return url
    if re.search(r"=s\d+", url):
        return re.sub(r"=s\d+", f"=s{size}", url)
    return url


def _fetch_chat_sync(video_id: str) -> list[dict]:
    """
    Fetch live chat messages using pytchat — exact logic from chats_fast.py.

    pytchat.create() + sync_items() is the working pattern.
    This runs in run_in_executor because pytchat is blocking.

    Fields ported from chats_fast.py:
      c.author.name, c.author.imageUrl, c.message,
      c.author.isChatOwner, c.author.isChatModerator, c.author.isChatSponsor,
      c.type (for membership milestones), c.author.badgeText
    """
    messages = []
    try:
        # ── FIX: pytchat.create() calls signal.signal(SIGINT, ...) internally
        # to set up a Ctrl+C handler. signal.signal() crashes with
        # "signal only works in main thread" when called from run_in_executor.
        # We don't need that handler in a bot — temporarily replace it with
        # a no-op, create the chat, then restore the original.
        _orig_signal = signal.signal
        signal.signal = lambda *a, **kw: None
        try:
            chat = pytchat.create(video_id=video_id)
        finally:
            signal.signal = _orig_signal

        # Give pytchat's internal fetch thread time to load the first batch.
        # Without this sleep, get() returns an empty buffer immediately because
        # we call it before the background thread has fetched anything — the
        # same reason c.py works (Python startup gives it natural lead time).
        time.sleep(7)

        if not chat.is_alive():
            return messages

        # From chats_fast.py: for c in chat.get().sync_items()
        # NOTE: with DefaultProcessor (what sync_items() gives you), the
        # Author object only exposes `.name` — there's no `.displayName`.
        # `.displayName` only shows up in the *raw dict* format you get from
        # CompatibleProcessor (e.g. c["authorDetails"]["displayName"]), which
        # is a different API shape entirely and isn't what's used here.
        # `.name` IS already the channel's display name, so this is correct.
        for c in chat.get().sync_items():
            messages.append({
                "author_name":  _display_name(c.author.name),
                "author_image": _hires_avatar(c.author.imageUrl, size=0),
                "message":      c.message or "",
                "is_owner":     c.author.isChatOwner,
                "is_mod":       c.author.isChatModerator,
                "is_member":    c.author.isChatSponsor,
                "is_milestone": c.type == "membershipItem",
                "badge_text":   getattr(c.author, "badgeText", None),
            })

    except Exception as e:
        logger.error(f"pytchat error: {e}")

    return messages


class LiveStreamChatModule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="livestream-chat",
        description="Fetch a recent message from a YouTube live stream chat 🎬",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="YouTube live stream URL or video ID",
        send_method="How to send the message",
        message_format="Send as a rich embed or plain text",
        target_channel="Channel to send the message to (defaults to current channel)",
    )
    @app_commands.choices(
        send_method=[
            app_commands.Choice(name="🤖 Send as Bot",               value="bot"),
            app_commands.Choice(name="🎭 Webhook (Chatter Identity)", value="webhook"),
        ],
        message_format=[
            app_commands.Choice(name="🖼️ Embed",      value="embed"),
            app_commands.Choice(name="💬 Plain Text", value="plain"),
        ],
    )
    async def livestream_chat(
        self,
        interaction: discord.Interaction,
        query: str,
        send_method:     app_commands.Choice[str] = None,
        message_format:  app_commands.Choice[str] = None,
        target_channel:  discord.TextChannel       = None,
    ):
        await interaction.response.defer(ephemeral=True)

        channel = target_channel or interaction.channel
        if not channel:
            err_embed = discord.Embed(
                title="❌ Channel Error",
                description="Cannot determine the active text channel. Please try again!",
                color=0xFF6B6B,
            )
            return await interaction.followup.send(embed=err_embed, ephemeral=True)

        send_as     = send_method.value    if send_method    else "bot"
        fmt         = message_format.value if message_format else "embed"
        loading_msg = None

        try:
            # ── Loading embed — ephemeral, only visible to user ────────────────
            loading_embed = discord.Embed(
                title="🔍 Searching for live stream...",
                description="Finding your stream on YouTube! 🎬\n\nThis might take a moment... ✨",
                color=random.choice(PASTEL_COLORS),
            )
            loading_embed.set_thumbnail(url=random.choice(LOADING_GIFS))
            loading_msg = await interaction.followup.send(embed=loading_embed, ephemeral=True)

            # ── Step 1: Resolve Video ID ───────────────────────────────────────
            if query.startswith("http://") or query.startswith("https://"):
                resolved = await resolve_url(query)
                video_id = extract_video_id(resolved)
            else:
                video_id = extract_video_id(query)

            if not video_id:
                if loading_msg:
                    try: await loading_msg.edit(content=None, embed=None)
                    except Exception: pass
                err_embed = discord.Embed(
                    title="❌ Invalid Input",
                    description=(
                        "Couldn't extract a video ID from:\n`" + query + "`\n\n"
                        "Please paste a YouTube URL or an 11-character video ID. 💕"
                    ),
                    color=0xFF6B6B,
                )
                return await interaction.followup.send(embed=err_embed, ephemeral=True)

            # ── Update loading embed ───────────────────────────────────────────
            updating_embed = discord.Embed(
                title="📡 Connecting to live chat...",
                description="Fetching the most recent message! 🎤\n\nAlmost there... ⏳",
                color=random.choice(PASTEL_COLORS),
            )
            updating_embed.set_thumbnail(url=random.choice(LOADING_GIFS))
            if loading_msg:
                try:
                    await loading_msg.edit(embed=updating_embed)
                except Exception:
                    pass

            # ── Step 2: Fetch chat via pytchat (in executor — it's blocking) ───
            loop     = asyncio.get_running_loop()
            messages = await loop.run_in_executor(
                None, lambda: _fetch_chat_sync(video_id)
            )

            if not messages:
                if loading_msg:
                    try: await loading_msg.edit(content=None, embed=None)
                    except Exception: pass
                err_embed = discord.Embed(
                    title="❌ No Messages Found",
                    description=(
                        "No chat messages came back for this stream. 😔\n\n"
                        "Make sure the stream is **currently live** and has chat enabled.\n"
                        "Stream ID: `" + video_id + "`"
                    ),
                    color=0xFF6B6B,
                )
                return await interaction.followup.send(embed=err_embed, ephemeral=True)

            # ── Step 3: Pick message & build badge (same role logic as chats_fast) ──
            recent       = messages[-1]
            author_name  = recent["author_name"]
            author_image = recent["author_image"]
            message_text = recent["message"] or "*(empty message)*"
            stream_url   = "https://youtube.com/watch?v=" + video_id

            # Badge mirrors chats_fast.py icon logic
            badge = ""
            if recent["is_owner"]:
                badge = " 👑"
            elif recent["is_mod"]:
                badge = " 🔧"
            elif recent["is_milestone"]:
                badge = " 🎊"
            elif recent["is_member"]:
                badge = " 💚"

            # ── Step 4: Build payload ──────────────────────────────────────────
            if fmt == "plain":
                send_content = message_text[:1900]
                send_embed   = None
            else:
                send_embed = discord.Embed(
                    title="🎬 Live Stream Chat Message",
                    description=message_text[:4000],
                    color=random.choice(PASTEL_COLORS),
                    timestamp=datetime.now(),
                )
                send_embed.add_field(
                    name="👤 Author",
                    value=author_name[:100] + badge,
                    inline=True,
                )
                send_embed.add_field(
                    name="🔗 Stream",
                    value="[Link](" + stream_url + ")",
                    inline=True,
                )
                if recent["badge_text"]:
                    send_embed.add_field(
                        name="🏅 Badge",
                        value=recent["badge_text"],
                        inline=True,
                    )
                if author_image:
                    send_embed.set_thumbnail(url=author_image)
                send_embed.set_footer(
                    text="YouTube Live Chat · via pytchat",
                    icon_url="https://www.youtube.com/favicon.ico",
                )
                send_content = None

            # ── Step 5: Send ───────────────────────────────────────────────────
            if send_as == "webhook":
                await self._send_via_webhook(
                    interaction=interaction,
                    channel=channel,
                    embed=send_embed,
                    content=send_content,
                    author_name=author_name,
                    author_image_url=author_image,
                )
            else:
                await channel.send(content=send_content, embed=send_embed)

            if loading_msg:
                try: await loading_msg.edit(content=None, embed=None)
                except Exception: pass

            success_embed = discord.Embed(
                title="✅ Message Fetched Successfully!",
                description=(
                    "Sent the latest message from **" + author_name + "** 🎉\n"
                    + ("📌 Sent to " + channel.mention if target_channel else "")
                ),
                color=0xB5EAD7,
            )
            await interaction.followup.send(embed=success_embed, ephemeral=True)

        except Exception as outer_err:
            if loading_msg:
                try: await loading_msg.edit(content=None, embed=None)
                except Exception: pass
            logger.error("Outer error: " + str(outer_err))
            err_embed = discord.Embed(
                title="❌ Something Went Wrong",
                description="An unexpected error occurred:\n`" + str(outer_err)[:100] + "`",
                color=0xFF6B6B,
            )
            await interaction.followup.send(embed=err_embed, ephemeral=True)

    async def _send_via_webhook(
        self,
        interaction: discord.Interaction,
        channel,
        embed,
        content,
        author_name:      str,
        author_image_url: str,
    ):
        """Send message via webhook with the chatter's identity 🎭"""
        if not interaction.guild or not hasattr(channel, "permissions_for"):
            await channel.send(content=content, embed=embed)
            return

        try:
            if not channel.permissions_for(interaction.guild.me).manage_webhooks:
                warn_embed = discord.Embed(
                    title="⚠️ Webhook Permission Missing",
                    description="Bot needs `Manage Webhooks` permission.\nSent as bot instead! 🤖",
                    color=0xFFB347,
                )
                await channel.send(content=content, embed=embed or warn_embed)
                return

            webhooks = await channel.webhooks()
            webhook  = None
            for wh in webhooks:
                if wh.name == "YT Live Chat" and wh.user == interaction.guild.me:
                    webhook = wh
                    break
            if not webhook:
                webhook = await channel.create_webhook(name="YT Live Chat")

            await webhook.send(
                content=content,
                embed=embed,
                username=author_name[:80],
                avatar_url=author_image_url or "https://www.youtube.com/favicon.ico",
            )

        except discord.Forbidden:
            perm_err = discord.Embed(
                title="❌ Permission Denied",
                description="Bot lacks permission to create or view webhooks.",
                color=0xFF6B6B,
            )
            await channel.send(embed=perm_err)
        except Exception as e:
            logger.error("Webhook error: " + str(e))
            warn_embed = discord.Embed(
                title="⚠️ Webhook Failed",
                description="Falling back to bot message...\n`" + str(e)[:100] + "`",
                color=0xFFB347,
            )
            await channel.send(content=content, embed=embed or warn_embed)


async def setup(bot):
    await bot.add_cog(LiveStreamChatModule(bot))
    print("✅ Live Stream Chat Module loaded successfully!")
