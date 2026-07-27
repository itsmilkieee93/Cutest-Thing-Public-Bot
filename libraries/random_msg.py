import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import asyncio
import random
from datetime import datetime, timezone

DISCORD_EPOCH_MS = 1420070400000


class RandomMsgCog(commands.Cog):
    """
    🌸 /random-msg — standalone roulette command. Talks to Discord's v10 REST
    API directly over its own aiohttp session, so this cog can be dropped
    into ANY bot.py (no dependency on EnchantedBot's custom helper methods).
    """

    def __init__(self, bot):
        self.bot     = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    # ── Discord v10 REST fetching (raw aiohttp) ─────────────────────────────
    async def _fetch_messages_page(self, channel_id: int, limit: int = 100,
                                    before: str | None = None, around: str | None = None,
                                    after: str | None = None) -> list:
        session = await self._get_session()
        url     = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {self.bot.http.token}"}
        params  = {"limit": limit}
        if around:
            params["around"] = around
        elif after:
            params["after"] = after
        elif before:
            params["before"] = before

        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 429:
                    data        = await resp.json()
                    retry_after = float(data.get("retry_after", 1))
                    await asyncio.sleep(retry_after + 0.1)
                    return await self._fetch_messages_page(channel_id, limit, before, around, after)
                if resp.status != 200:
                    print(f"⚠️ [random-msg] fetch endpoint returned {resp.status}")
                    return []
                return await resp.json()
        except Exception as e:
            print(f"⚠️ [random-msg] fetch page error: {e}")
            return []

    async def _fetch_recent_messages(self, channel_id: int, total: int = 1000,
                                      page_size: int = 100, around: str | None = None,
                                      direction: str = "before") -> list:
        all_messages = []
        cursor       = None
        first_page   = True

        while len(all_messages) < total:
            remaining = total - len(all_messages)
            limit     = min(page_size, remaining)

            if first_page and around:
                page = await self._fetch_messages_page(channel_id, limit=limit, around=around)
            elif direction == "after":
                page = await self._fetch_messages_page(channel_id, limit=limit, after=cursor)
            else:
                page = await self._fetch_messages_page(channel_id, limit=limit, before=cursor)
            first_page = False

            if not page:
                break

            all_messages.extend(page)
            cursor = page[0]["id"] if direction == "after" else page[-1]["id"]

            if len(page) < limit:
                break
            await asyncio.sleep(0.3)  # be gentle between paginated requests

        return all_messages

    def _timestamp_to_snowflake(self, timestamp_ms: int) -> str:
        return str(int(timestamp_ms - DISCORD_EPOCH_MS) << 22)

    def _pick_roulette_window(self, channel: discord.abc.Messageable) -> tuple[str, str | None]:
        """🌸 Same six equal-odds modes as the mention-reaction roulette."""
        mode = random.choice(["recent", "yesterday", "week", "month", "anytime", "nostalgia"])
        if mode == "recent":
            return mode, None

        now_ms           = discord.utils.utcnow().timestamp() * 1000
        channel_created  = getattr(channel, "created_at", discord.utils.utcnow())
        channel_start_ms = channel_created.timestamp() * 1000
        max_age_ms       = max(now_ms - channel_start_ms, 3600 * 1000)
        day_ms           = 24 * 3600 * 1000

        if mode == "yesterday":
            offset_ms = random.uniform(1 * day_ms, 2 * day_ms)
        elif mode == "week":
            offset_ms = random.uniform(1 * day_ms, 7 * day_ms)
        elif mode == "month":
            offset_ms = random.uniform(7 * day_ms, 30 * day_ms)
        elif mode == "anytime":
            offset_ms = random.uniform(1 * day_ms, max_age_ms)
        else:  # nostalgia — skewed toward the old end of history
            r         = random.random() ** 0.4
            offset_ms = r * max_age_ms

        offset_ms = min(offset_ms, max_age_ms)
        pivot_ms  = max(now_ms - offset_ms, channel_start_ms)
        return mode, self._timestamp_to_snowflake(int(pivot_ms))

    def _pick_random_message(self, messages: list, exclude_id: str | None = None) -> dict | None:
        candidates = [
            m for m in messages
            if m.get("id") != exclude_id
            and m.get("type", 0) in (0, 19)                    # DEFAULT or REPLY only
            and not m.get("author", {}).get("bot", False)      # 🚫 no bot messages
            and (m.get("content") or m.get("embeds") or m.get("attachments"))
        ]
        return random.choice(candidates) if candidates else None

    # ── metadata helpers ─────────────────────────────────────────────────────
    def _avatar_url(self, author: dict) -> str:
        user_id     = author.get("id")
        avatar_hash = author.get("avatar")
        if avatar_hash:
            ext = "gif" if avatar_hash.startswith("a_") else "png"
            return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size=256"
        # No custom avatar → Discord's default pfp
        discriminator = author.get("discriminator", "0")
        index = (int(discriminator) % 5) if discriminator and discriminator != "0" else (int(user_id) >> 22) % 6
        return f"https://cdn.discordapp.com/embed/avatars/{index}.png"

    def _jump_url(self, guild_id, channel_id, message_id) -> str:
        root = f"https://discord.com/channels/{guild_id or '@me'}"
        return f"{root}/{channel_id}/{message_id}"

    def _format_sent_at(self, iso_timestamp: str) -> tuple[str, str]:
        """🌸 Raw v10 ISO timestamp → (DD-MM-YYYY, HH:MM:SS) in UTC."""
        dt = datetime.fromisoformat(iso_timestamp).astimezone(timezone.utc)
        return dt.strftime("%d-%m-%Y"), dt.strftime("%H:%M:%S")

    # ── command ──────────────────────────────────────────────────────────────
    @app_commands.command(name="random-msg", description="🎲 Pulls a random message from this channel's history")
    async def random_msg(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        channel = interaction.channel

        mode, pivot = self._pick_roulette_window(channel)
        direction   = random.choice(["before", "after"]) if pivot else "before"

        messages = await self._fetch_recent_messages(channel.id, total=1000, around=pivot, direction=direction)
        if not messages and pivot:
            messages = await self._fetch_recent_messages(channel.id, total=1000)

        picked = self._pick_random_message(messages)
        if not picked:
            await interaction.followup.send("😭 Couldn't find a message to pull from this channel yet!")
            return

        author      = picked.get("author", {})
        username    = author.get("username", "unknown")
        global_name = author.get("global_name") or username
        avatar_url  = self._avatar_url(author)

        content         = (picked.get("content") or "").strip()
        attachment_urls = [a["url"] for a in picked.get("attachments", []) if a.get("url")]

        # 🌸 Attachments + text content go ABOVE the embed (attachments first
        # so Discord unfurls them properly), exactly like a real forwarded
        # message — the embed underneath is pure metadata.
        top_parts     = attachment_urls + ([content] if content else [])
        reply_content = "\n".join(top_parts)[:2000] if top_parts else None

        date_str, time_str = self._format_sent_at(picked.get("timestamp"))
        jump_url = self._jump_url(interaction.guild_id, channel.id, picked.get("id"))

        embed = discord.Embed(color=0xFF6EC7, timestamp=discord.utils.utcnow())  # 🌸 neon pink brand color
        embed.set_author(name=f"{global_name} (@{username})", icon_url=avatar_url)
        embed.set_thumbnail(url=avatar_url)
        embed.add_field(name="👤 Username", value=f"@{username}", inline=True)
        embed.add_field(name="🏷️ Global Name", value=global_name, inline=True)
        embed.add_field(name="📅 Sent", value=f"{date_str}\n{time_str} UTC", inline=True)
        embed.set_footer(text=f"🎲 Roulette mode: {mode}")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="🔗 Jump to Message",
            style=discord.ButtonStyle.link,
            url=jump_url,
        ))

        await interaction.followup.send(content=reply_content, embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(RandomMsgCog(bot))
