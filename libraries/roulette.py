import re
import random
import discord
import asyncio
from groq_instruct import (
    DISCORD_EPOCH_MS, CONTEXT_TRIGGERS, KEYWORD_STOPWORDS,
    REACT_TAG_PATTERN, EXPLICIT_EMOJI_PATTERN
)


class RouletteService:
    """
    🌸 Handles all message roulette logic — pulling random/historical messages
    from channels and resending them as contextual or random replies to mention triggers.
    """
    
    def __init__(self):
        pass

    def _timestamp_to_snowflake(self, timestamp_ms: int) -> str:
        """🌸 Converts a Unix timestamp (ms) into a Discord snowflake usable with `around`/`before`."""
        return str(int(timestamp_ms - DISCORD_EPOCH_MS) << 22)

    def _pick_roulette_window(self, channel: discord.abc.Messageable) -> tuple[str, str | None]:
        """
        🌸 Randomly rolls a roulette mode and returns (mode_name, pivot_snowflake).
        pivot=None means "recent" — just grab the newest messages like before.
        Otherwise the pivot is a snowflake to center the fetch on via `around`.

        Modes (equal chance each — no bias toward "recent" anymore):
          - recent    : latest messages (original behavior)
          - yesterday : somewhere in the last ~24-48h
          - week      : somewhere in the last ~1-7 days
          - month     : somewhere in the last ~7-30 days
          - anytime   : anywhere across the channel's entire history
          - nostalgia : anytime, but weighted toward the OLDEST messages
        """
        mode = random.choice(
            ["recent", "yesterday", "week", "month", "anytime", "nostalgia"]
        )

        if mode == "recent":
            return mode, None

        now_ms           = discord.utils.utcnow().timestamp() * 1000
        channel_created  = getattr(channel, "created_at", discord.utils.utcnow())
        channel_start_ms = channel_created.timestamp() * 1000
        max_age_ms       = max(now_ms - channel_start_ms, 3600 * 1000)  # at least 1h of headroom

        day_ms = 24 * 3600 * 1000

        if mode == "yesterday":
            offset_ms = random.uniform(1 * day_ms, 2 * day_ms)
        elif mode == "week":
            offset_ms = random.uniform(1 * day_ms, 7 * day_ms)
        elif mode == "month":
            offset_ms = random.uniform(7 * day_ms, 30 * day_ms)
        elif mode == "anytime":
            offset_ms = random.uniform(1 * day_ms, max_age_ms)
        else:  # nostalgia — skew toward the old end of the channel's history
            r         = random.random() ** 0.4  # closer to 1 = closer to the old end
            offset_ms = r * max_age_ms

        offset_ms    = min(offset_ms, max_age_ms)  # never reach before the channel existed
        pivot_ms     = max(now_ms - offset_ms, channel_start_ms)
        pivot        = self._timestamp_to_snowflake(int(pivot_ms))

        return mode, pivot

    def pick_random_message(self, messages: list, exclude_id: str | None = None, pattern: re.Pattern | None = None) -> dict | None:
        """
        🌸 Filters raw v10 message dicts down to ones worth resending
        (has text, embeds, OR media/file attachments, is a normal/reply
        message, isn't the trigger message itself) then picks one at random.
        If `pattern` is given, only messages whose content matches it are
        kept — this is what powers the context-aware "find another message
        that said the same kind of thing" roulette.
        """
        candidates = [
            m for m in messages
            if m.get("id") != exclude_id
            and m.get("type", 0) in (0, 19)                     # DEFAULT or REPLY only
            and not m.get("author", {}).get("bot", False)       # 🚫 no bot messages
            and (m.get("content") or m.get("embeds") or m.get("attachments"))  # text, embed, or media
            and (pattern is None or pattern.search(m.get("content") or ""))
        ]
        if not candidates:
            return None
        return random.choice(candidates)

    async def _send_random_message_reply(self, message: discord.Message, fetch_recent_messages_v10, reply_with_picked_message):
        """
        🌸 Rolls a random time-window mode (recent / yesterday / week / month /
        anytime / nostalgia), pulls up to 1000 messages from that point in the
        channel via the v10 REST endpoint (paginated), picks a random one
        (excluding bots), and replies with its content/embeds/media.
        NOTE: does not manage its own typing pulse — the caller owns that.
        """
        mode, pivot = self._pick_roulette_window(message.channel)

        # 🎲 When we have a pivot, also randomize which side of it we scan —
        # older ("before") or newer ("after") — so the same time window
        # doesn't always sample the same chunk of history.
        direction = random.choice(["before", "after"]) if pivot else "before"
        messages  = await fetch_recent_messages_v10(
            message.channel.id, total=1000, around=pivot, direction=direction
        )

        # Windowed pivot came up dry (e.g. very young channel) — fall back to recent
        if not messages and pivot:
            messages = await fetch_recent_messages_v10(message.channel.id, total=1000)

        picked = self.pick_random_message(messages, exclude_id=str(message.id))

        if not picked:
            return

        await reply_with_picked_message(message, picked)

    async def _reply_with_picked_message(self, message: discord.Message, picked: dict):
        """
        🌸 Shared "resend this picked v10 message dict as a reply" logic —
        rebuilds rich embeds, surfaces media/attachment URLs, truncates to
        2000 chars, neuters @everyone/@here/role pings, and sends after a
        human-like delay. Used by both the plain random roulette and the
        context-matched (keyword) roulette so the formatting stays identical.
        """
        content = picked.get("content") or ""

        # 🖼️ Only rebuild "rich" embeds (custom ones made by a bot/webhook,
        # e.g. via /embed-msg). Discord's OWN auto-generated embeds for links
        # (type "image", "video", "gifv", "link", "article") render broken/
        # tiny when a bot resends them manually — the fix is to leave those
        # alone and let Discord re-unfurl the URL fresh from `content` below,
        # which gives the full, proper embed instead of a cramped thumbnail.
        embeds = [
            discord.Embed.from_dict(e)
            for e in picked.get("embeds", [])
            if e.get("type") == "rich"
        ][:10]

        # 🖼️ Media/attachments (images, gifs, videos, files) — put their CDN
        # URLs first so Discord unfurls them and they're never cut off by
        # the 2000-char truncation below. Also covers plain "link messages"
        # since a URL-only message is just content.
        attachment_urls = [a["url"] for a in picked.get("attachments", []) if a.get("url")]
        if attachment_urls:
            links_block = "\n".join(attachment_urls)
            content = f"{links_block}\n{content}".strip() if content else links_block

        content = content[:2000]

        # 🚫 If the picked message contains @everyone/@here (or a role ping),
        # never actually fire that notification when we resend it — only the
        # person who triggered the roulette gets pinged (the reply mention).
        safe_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=True,
            replied_user=True,
        )

        # Human-like delay before replying
        await asyncio.sleep(random.uniform(1, 5))

        await message.reply(
            content=content if content else None,
            embeds=embeds if embeds else [],
            allowed_mentions=safe_mentions,
        )

    def _match_context_category(self, content: str) -> str | None:
        """
        🌸 Scans a mention/reply-to-bot message for common conversational
        cues (greetings, thanks, farewells, "how are you", etc.) using
        whole-word regex matching. Returns the matched CATEGORY KEY (e.g.
        "greeting") if something matches, or None if nothing does (→ caller
        falls back to the plain random message roulette instead).
        """
        if not content:
            return None

        # Strip user mentions and custom emoji so they don't interfere
        clean = re.sub(r"<@!?\d+>|<a?:\w+:\d+>", "", content).strip()
        if not clean:
            return None

        for key, category in CONTEXT_TRIGGERS.items():
            if category["pattern"].search(clean):
                return key

        return None

    def _extract_keywords(self, content: str) -> list[str]:
        """
        🌸 Pulls meaningful words out of a mention/reply message that DIDN'T
        match any curated CONTEXT_TRIGGERS category — this is what lets
        "same thing for other words" work generically, not just for the
        curated hi/thanks/bye/etc. list. Strips mentions/emoji/links, keeps
        words 4+ letters, dedupes, and drops common filler via
        KEYWORD_STOPWORDS so the match stays meaningful.
        """
        if not content:
            return []

        clean = re.sub(r"<@!?\d+>|<a?:\w+:\d+>|https?://\S+", "", content)
        words = re.findall(r"[A-Za-z']{4,}", clean.lower())
        return [w for w in dict.fromkeys(words) if w not in KEYWORD_STOPWORDS][:8]

    async def _send_keyword_matched_reply(self, message: discord.Message, keywords: list[str], fetch_recent_messages_v10, reply_with_picked_message) -> bool:
        """
        🌸 Generic word-overlap roulette. Scans up to 1000 recent channel
        messages for ANY other message containing at least one of `keywords`
        (whole-word match) and sends it back. Even just ONE matching message
        in the channel is enough — no minimum count needed, first match wins.
        Returns True if something was found & sent; False means the caller
        should fall back to the plain random roulette instead.
        """
        if not keywords:
            return False

        pattern  = re.compile(r"\b(" + "|".join(re.escape(w) for w in keywords) + r")\b", re.IGNORECASE)
        messages = await fetch_recent_messages_v10(message.channel.id, total=1000)
        picked   = self.pick_random_message(messages, exclude_id=str(message.id), pattern=pattern)

        if not picked:
            return False

        await reply_with_picked_message(message, picked)
        return True

    async def _send_context_matched_reply(self, message: discord.Message, category_key: str, fetch_recent_messages_v10, reply_with_picked_message):
        """
        🌸 Context-aware trigger reply. When the mention/reply matches a known
        category (e.g. "greeting" for "hi"), scan up to 1000 recent channel
        messages for OTHER messages matching that SAME category's pattern —
        so someone saying "hi" fishes up someone else's "hi"/"hello"/"yo" from
        the channel history and sends it back, instead of always the same
        canned line. Falls back to a canned reply from CONTEXT_TRIGGERS if no
        matching message is found in the scanned history.
        """
        pattern  = CONTEXT_TRIGGERS[category_key]["pattern"]
        messages = await fetch_recent_messages_v10(message.channel.id, total=1000)
        picked   = self.pick_random_message(messages, exclude_id=str(message.id), pattern=pattern)

        if picked:
            await reply_with_picked_message(message, picked)
        else:
            safe_mentions = discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=True
            )
            await asyncio.sleep(random.uniform(1, 3))  # human-like delay
            fallback = random.choice(CONTEXT_TRIGGERS[category_key]["replies"])
            await message.reply(fallback, allowed_mentions=safe_mentions)
