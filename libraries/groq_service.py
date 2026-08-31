"""🌸 groq_service.py

Mention/reply handling + server-question dispatch, split out of
bot_service.py's EnchantedBot god-class.

Owns:
  - The mention/reply reaction pipeline (handle_mention_reaction) —
    regex server-question interceptors → media-request interceptor →
    Groq AI fallback → emoji reactions → reply.
  - Reply-context resolution (formal reply chains + implicit "posted
    then pinged in a follow-up" context).
  - Guild data syncing into metadata.db/roles.db/channels.db, and the
    lightweight "where am I" identity line fed to Groq — both existed
    only to feed guild context into the mention/question pipeline, so
    they live here now instead of on the bot class.

EnchantedBot (bot_service.py) owns a GroqMentionService instance and
delegates on_message/on_ready/on_guild_join hooks into it. Discord
gateway wiring, presence/status, network watchdog, DM forwarding, and
slash-command registration all stay in bot_service.py — this file is
purely the "someone @mentioned/replied to the bot, or asked a
server-info question" path.
"""

import time
import random
import re
import discord
from datetime import datetime

from groq_instruct import (
    handle_role_query, handle_created_query, handle_server_info_query,
    handle_channel_count_query, handle_role_list_query,
    handle_server_avatar_query, handle_server_banner_query,
    handle_server_owner_query, handle_server_verification_query,
    handle_member_count_query, handle_server_age_query,
    handle_boost_status_query, handle_locale_query,
    handle_user_created_query,
    handle_server_description_query, handle_all_metadata_query,
    REACT_TAG_PATTERN, REACT_REQUEST_PATTERN,
    RECENT_EMOJI_MEMORY, AUTO_REACT_CHANCE,
    _looks_server_related,
)
from groq_pexels import handle_media_request
from groq_music_suggestion import (
    handle_music_request, save_music_interaction,
    MusicPaginatorView, _track_urls,
)
from extras.groq_dm_instruct import DM_REQUEST_PATTERN, route_dm_split
from extras.groq_attachments import get_image_attachments, describe_attachments
from resources import shared

# 🌸 Referential word gate for reply-context folding — see the comment
# in handle_mention_reaction where this is used. Compiled once at import
# time instead of per-call.
_REFERENTIAL_WORD_PATTERN = re.compile(r"\b(it|that|this|its|it's)\b", re.IGNORECASE)

# 🔔 Reply-ping policy — pings ONLY the person being replied to (the
# trigger). @everyone/@here and role mentions never notify anyone even if
# they somehow end up in the reply text, and neither do any OTHER user
# mentions the AI-generated text might contain (accidental @-ing a
# bystander). Shared by every message.reply() call in handle_mention_reaction.
SAFE_REPLY_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=True)

# 🌸 How long a guild's lightweight identity-line cache stays valid
# before get_server_context_text re-derives it. See get_server_context_text.
GUILD_INFO_CACHE_TTL = 300  # 5 minutes


class GroqMentionService:
    """🌸 Handles @mentions/replies to the bot, server-info question
    interceptors, and the guild-data cache/sync those interceptors read
    from. One instance lives on EnchantedBot (self.groq_mentions)."""

    def __init__(self, bot):
        self.bot = bot

        # 🌸 channel_id -> deque of the last RECENT_EMOJI_MEMORY emoji this
        # channel auto-reacted with (both Groq-picked and directly-requested
        # ones — see _record_reaction_emoji / _apply_emoji_reactions). Fed
        # back into get_ai_response as an "avoid these" list so the bot
        # stops defaulting to the same emoji (e.g. 🤗) over and over.
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

    # ─────────────────────────────────────────────────────────────────
    # Guild data sync / cache
    # ─────────────────────────────────────────────────────────────────

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
            if not guild.chunked and self.bot.intents.members:
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
        if not self.bot.guilds:
            print("⚠️ sync_all_guilds: no guilds available yet.")
            return

        print(f"🔄 Syncing full guild data for {len(self.bot.guilds)} guild(s)...")
        synced = 0
        for guild in self.bot.guilds:
            await self.sync_guild_to_db(guild)
            synced += 1
        print(f"✅ Synced {synced}/{len(self.bot.guilds)} guild(s) to metadata.db/roles.db/channels.db")

    def get_server_context_text(self, guild: discord.Guild) -> str:
        """🌸 Return a lightweight "where am I" identity line for Groq — just
        server name + member count, NOT the full roles/channels dump. The
        detailed compact summary (top roles, channels) is pulled from the
        SQLite cache (metadata.db/roles.db/channels.db) via
        shared.get_guild_context_summary inside groq_ai.get_ai_response
        instead, so it's DB-backed and token-efficient rather than a live
        guild.roles/guild.channels dump that grows unbounded on big servers.
        """
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

    # ─────────────────────────────────────────────────────────────────
    # Reply-chain / context resolution
    # ─────────────────────────────────────────────────────────────────

    async def is_reply_to_bot(self, message: discord.Message) -> bool:
        """🌸 True if `message` is a reply to one of the bot's messages."""
        if not message.reference:
            return False

        try:
            replied_to = await message.channel.fetch_message(message.reference.message_id)
            return replied_to.author.id == self.bot.user.id
        except Exception:
            return False

    @staticmethod
    def _extract_message_text(msg: discord.Message) -> str:
        """🌸 Pulls readable text out of a discord.Message regardless of
        whether the bot sent it as plain content or an embed — the
        server-info/avatar/owner/etc. interceptors in this file all reply
        with discord.Embed, not plain text, so msg.content alone would be
        empty for those. Joins embed title/description/fields into one
        flat string so it reads naturally when dropped into a prompt.
        Used by _reply_to_bot_context below to give Groq the ACTUAL
        content of an old bot message when that message was never saved
        into Groq's own memory.db (true for every interceptor reply,
        since they short-circuit BEFORE get_ai_response is ever called —
        see handle_mention_reaction).
        """
        parts = []
        if msg.content:
            parts.append(msg.content.strip())

        for embed in msg.embeds:
            if embed.title:
                parts.append(embed.title.strip())
            if embed.description:
                parts.append(embed.description.strip())
            for field in embed.fields:
                name  = (field.name or "").strip()
                value = (field.value or "").strip()
                if name or value:
                    parts.append(f"{name}: {value}".strip(": "))

        return "\n".join(p for p in parts if p).strip()

    @staticmethod
    def _extract_forwarded_text(message: discord.Message) -> str | None:
        """🌸 Pulls readable text out of a Discord-native FORWARDED message
        (the "Forward" share feature, not a reply). Forwarded messages
        arrive with an empty message.content and the actual payload
        living in message.message_snapshots — a list of
        discord.MessageSnapshot, each carrying its own .content/.embeds/
        .attachments/.stickers from the ORIGINAL message being forwarded.
        Without this, a forward looks like a blank message to Groq (since
        clean_content on the wrapper message is empty) and the bot has
        nothing to respond to.

        Mirrors _extract_message_text's embed-flattening logic, but reads
        from a MessageSnapshot instead of a full discord.Message since
        snapshots don't carry an author/channel/id of their own — just
        the content payload. Attachments/stickers are called out by name
        (not content) since there's no direct download hook here; that
        stays consistent with how the rest of this file treats media —
        described, not fetched — in the AI prompt path.

        Returns None if `message` has no forwarded snapshots at all, so
        callers can cheaply no-op for the (overwhelming) common case of a
        normal message/reply.
        """
        snapshots = getattr(message, "message_snapshots", None)
        if not snapshots:
            return None

        blocks = []
        for snap in snapshots:
            parts = []
            if snap.content:
                parts.append(snap.content.strip())

            for embed in snap.embeds:
                if embed.title:
                    parts.append(embed.title.strip())
                if embed.description:
                    parts.append(embed.description.strip())
                for field in embed.fields:
                    name  = (field.name or "").strip()
                    value = (field.value or "").strip()
                    if name or value:
                        parts.append(f"{name}: {value}".strip(": "))

            if snap.attachments:
                names = ", ".join(a.filename for a in snap.attachments)
                parts.append(f"[attachment(s): {names}]")

            if snap.stickers:
                names = ", ".join(s.name for s in snap.stickers)
                parts.append(f"[sticker(s): {names}]")

            block = "\n".join(p for p in parts if p).strip()
            if block:
                blocks.append(block)

        if not blocks:
            return None

        # 🌸 Discord currently only ever sends a single snapshot per
        # forwarded message reference, but the API models it as a list —
        # join defensively in case that ever changes.
        return "\n---\n".join(blocks)

    @classmethod
    def _extract_full_text(cls, msg: discord.Message) -> str:
        """🌸 Combines _extract_message_text (content + embeds) with
        _extract_forwarded_text (message_snapshots) into one string — the
        "everything readable about this message" helper. A forward SENT
        WITH a typed comment carries both halves on the same
        discord.Message: msg.content is the comment, the actual forwarded
        payload lives separately in msg.message_snapshots. Using only
        _extract_message_text (as _reply_to_bot_context did before this)
        misses the forwarded content entirely and only sees the comment —
        or nothing, if the forward was sent with no comment at all.
        Forwarded content is listed first since it's usually the more
        substantial part of the message.
        """
        forwarded = cls._extract_forwarded_text(msg)
        own       = cls._extract_message_text(msg)
        return "\n".join(p for p in (forwarded, own) if p).strip()

    async def _reply_to_bot_context(self, message: discord.Message) -> tuple[int | None, str | None]:
        """🌸 Returns (message_id, text) for whatever message `message` is
        replying to — the snowflake to anchor Groq's random memory slice
        around (see groq_ai.get_ai_response's reply_to_message_id param),
        and the ACTUAL readable content of that old message as a
        fallback for when it was never saved into Groq's own memory.db
        at all — true for every interceptor reply (avatar/owner/server-info/
        media/etc. in handle_mention_reaction above), since those
        short-circuit and reply BEFORE get_ai_response is ever called, so
        they have no history row to anchor onto. See groq_ai's FALLBACK
        CONTEXT block for how reply_to_message_text gets used in that
        case. Returns (None, None) if `message` isn't a reply at all, or
        the replied-to message has no extractable text/embed content.

        🌸 NOT JUST BOT REPLIES: originally this bailed unless the replied-
        to message was authored by Cutest Thing itself, so replying to
        e.g. another bot's embed (a Tracky subscriber-update card, a
        different bot's announcement) left Groq with zero context — just
        "look!" and nothing to look AT. Now any replied-to message with
        real content (text OR embed, from anyone/anything) is extracted,
        since the whole point is giving Groq something to answer about,
        not verifying who posted it.

        🌸 FAST PATH: message.reference.resolved is already populated by
        discord.py for most replies (Discord sends the replied-to message
        inline with the gateway payload) — checked FIRST to skip the
        extra fetch_message() API call. Falls through to fetch_message()
        only when resolved is missing/stale (very old message discord.py's
        cache dropped, or a discord.DeletedReferencedMessage stub).

        🌸 FORWARDED CONTENT: uses _extract_full_text (not the plain
        _extract_message_text) so that replying to a FORWARDED message
        also pulls in the actual forwarded payload from
        message_snapshots — not just whatever comment (if any) was typed
        alongside the forward. See _extract_full_text for why this needs
        to be a separate merge step.

        🌸 TRIGGER RESOLUTION: only attempted when the replied-to message
        is OUR OWN — bot replies are always sent via message.reply(...),
        so replied_to.reference (if present) points back to the ORIGINAL
        user message that triggered it — the actual question ("The Earth
        Diameter is", "give me an anime image") that produced this bot
        answer. Without this, the fallback text is just the bot's half of
        the exchange ("about 12,742 km" on its own, with no clue what
        "12,742 km" is even an answer TO). We resolve that trigger the
        same resolved-first/fetch-fallback way and prepend it, so the
        fallback context reads as a full Q→A pair instead of a dangling
        answer. Skipped for third-party messages (other bots/users) since
        there's no such reply-chain guarantee to walk — their own
        content stands alone as the context instead.
        """
        ref = message.reference
        if not ref or not ref.message_id:
            return None, None

        resolved = ref.resolved
        replied_to = resolved if isinstance(resolved, discord.Message) else None

        if replied_to is None:
            try:
                replied_to = await message.channel.fetch_message(ref.message_id)
            except Exception:
                return None, None

        answer_text = self._extract_full_text(replied_to)

        # 🌸 Walk one hop further back: what user message did THIS bot
        # message reply to? Only applies when replied_to is our OWN
        # message — see TRIGGER RESOLUTION above. If found, prepend it
        # so the fallback shows the question and the answer together.
        if replied_to.author.id == self.bot.user.id:
            trigger_ref = replied_to.reference
            if trigger_ref and trigger_ref.message_id:
                trigger_msg = trigger_ref.resolved if isinstance(trigger_ref.resolved, discord.Message) else None
                if trigger_msg is None:
                    try:
                        trigger_msg = await message.channel.fetch_message(trigger_ref.message_id)
                    except Exception:
                        trigger_msg = None

                if trigger_msg is not None and trigger_msg.author.id != self.bot.user.id:
                    question_text = self._extract_full_text(trigger_msg)
                    if question_text:
                        answer_text = f"{trigger_msg.author.display_name} asked: {question_text}\nAnswer: {answer_text}"

        # 🌸 Nothing extractable (e.g. replied to a bare attachment-only
        # message with no embed) — behave as if this wasn't a reply at
        # all rather than injecting an empty fallback block.
        if not answer_text:
            return None, None

        return replied_to.id, answer_text

    async def _implicit_reply_context(self, message: discord.Message) -> tuple[int | None, str | None]:
        """🌸 Fallback for when someone forwards/posts something and then
        pings the bot in a SEPARATE follow-up message instead of formally
        (swipe-)replying to it — e.g. forward a Tracky subscriber-update
        embed, then send "@Cutest Thing look!!" as its own message right
        after. Discord visually groups these two messages together (no
        repeated avatar/timestamp since they're back-to-back from the
        same author) but they're unrelated discord.Message objects with
        NO reference link between them — message.reference is None on
        the "look!!" message, so _reply_to_bot_context finds nothing at
        all and the bot has no idea what "look!!" refers to.

        This peeks at shared.msg_cache[channel_id] — the rolling last-5-
        messages-per-channel cache already populated unconditionally in
        on_message for every message, mention or not — for whatever
        landed immediately BEFORE this one, and treats it as implied
        context ONLY if both hold:
          - same author as the current mention (so we're not randomly
            grabbing a stranger's unrelated message out of the channel)
          - sent within the last few minutes (so an old forward from an
            hour ago doesn't get treated as "the thing they're asking
            about" for some unrelated later ping)
        Returns (None, None) if the cache is too short, the previous
        message fails either check, or it has nothing extractable.
        """
        cid    = str(message.channel.id)
        cached = shared.msg_cache.get(cid, [])

        # 🌸 on_message inserts THIS message into the cache at index 0
        # before handle_mention_reaction ever runs, so the message right
        # before it in the channel is index 1, not 0.
        if len(cached) < 2:
            return None, None

        prev = cached[1]

        if prev.id == message.id or prev.author.id != message.author.id:
            return None, None

        if (message.created_at - prev.created_at).total_seconds() > 180:
            return None, None

        text = self._extract_full_text(prev)
        if not text:
            return None, None

        return prev.id, text

    # ─────────────────────────────────────────────────────────────────
    # Mention / reply reaction pipeline
    # ─────────────────────────────────────────────────────────────────

    async def handle_mention_reaction(self, message: discord.Message):
        """🌸 Mention / reply reaction handler (e.g. random message roulette).
        Combines message content + Groq AI response + emoji reaction.
        """
        async with message.channel.typing():
            guild = message.guild

            # 🌸 Resolve reply context EARLY (moved up from below the
            # server-query dispatch block) — a follow-up like "when did it
            # get made" or "when did it was made" has NO server/guild
            # keyword in message.content on its own; "it" only resolves
            # once you know the PREVIOUS message was about the server.
            # Without this, _looks_server_related/classify_server_query/
            # the regex chain all only ever saw the bare current message
            # and had no way to know "it" = the server, so the question
            # fell through to the generic chat model, which has no access
            # to metadata.db and just guesses/hallucinates "idk, discord
            # doesn't show that" instead of answering from the DB.
            reply_to_message_id, reply_to_message_text = await self._reply_to_bot_context(message)
            if reply_to_message_id is None:
                reply_to_message_id, reply_to_message_text = await self._implicit_reply_context(message)

            # 🌸 Combined text used ONLY for server-query hint/classification/
            # regex matching below — message.clean_content sent to Groq
            # chat later is untouched, so this doesn't change what the
            # user "said" from the AI's perspective, just what the
            # interceptors are allowed to look at when deciding intent.
            #
            # 🌸 IMPORTANT: reply_to_message_text is ONLY folded in when
            # the current message contains a referential word (it/that/
            # this/etc) AND still doesn't look server-related without it.
            # Reply context exists to rescue a genuinely ambiguous
            # follow-up like "when did it get made" — "it" is a dangling
            # reference that only resolves via the previous message.
            #
            # Without the referential-word check, EVERY reply to one of
            # the bot's OWN server-info answers looked server-related
            # forever after, no matter what the user actually typed —
            # "cool", "lol", a plain "?" — because reply_to_message_text
            # is the BOT's own wording, which is packed with "server"/
            # "created"/"old" by definition. That made every follow-up
            # in that reply chain re-trigger the exact same copy-pasted
            # answer instead of falling through to normal conversation.
            # 🌸 FIX: try the CURRENT message alone first. If it already
            # looks server-related by itself (e.g. "is that cool server" —
            # contains "that" AND "server"), it's almost certainly a NEW
            # question, not a dangling reference to the bot's last answer.
            # Folding reply_to_message_text in on top of an already-
            # server-shaped message just drowns the classifier in the
            # bot's OLD wording and re-triggers the same handler on every
            # reply in that thread. Reply context is now only folded in
            # when the message DOESN'T look server-related on its own —
            # the actual "dangling pronoun" case ("when did it get made"
            # has no server keyword by itself) is unaffected.
            dispatch_text = message.content
            if (
                reply_to_message_text
                and not _looks_server_related(message.content)
                and _REFERENTIAL_WORD_PATTERN.search(message.content)
            ):
                dispatch_text = f"{reply_to_message_text}\n{message.content}"

            # 🌸 SERVER-QUERY DISPATCH — AI-FIRST, regex-FALLBACK.
            #
            # 0. server_hint is a zero-token LOCAL keyword gate checked
            #    BEFORE spending a Groq call on classify_server_query.
            #    Casual chat like "ur cute 🥺" or "lol how are u" has no
            #    server-ish keywords at all, so it skips that call entirely
            #    — same end behavior as a "none" classification (falls
            #    through to the regex chain below), just zero API cost for
            #    the majority of mentions that were never going to be
            #    server questions anyway.
            #    NOTE: this only gates the classifier call itself. The
            #    regex chain and handle_media_request below still run for
            #    EVERY guild message regardless of server_hint — media
            #    requests ("send me a pic of X") have nothing to do with
            #    server metadata and must not be skipped by this gate.
            # 1. Only when server_hint is True: classify_server_query makes
            #    ONE cheap Groq call (smallest model, 6 output tokens) to
            #    label the message. A label hit dispatches straight to
            #    that ONE handler — no need to run the other 13 regexes
            #    first, so paraphrases the regexes would've missed ("who
            #    owns this place", "how old is this discord") still land
            #    on the right handler.
            # 2. If the classifier returns "none" (not server-related, or
            #    the call errored/timed out/gave a junk label — see
            #    classify_server_query's fail-open contract), we fall
            #    through to the EXACT SAME regex chain as before, in the
            #    same most-specific-first order. Nothing above is removed;
            #    this only skips it on a successful AI classification.
            # 🌸 guild.id is only safe to access when guild is truthy —
            # DMs have guild=None. handle_media_request/handle_music_request
            # below run for EVERY message (guild or DM) and only use this
            # for logging, so 0 is a safe DM placeholder (never a real
            # guild id). Defined once here so the whole block below doesn't
            # need repeated `guild.id if guild else 0` inline.
            guild_id = guild.id if guild else 0

            # 🌸 intercepted MUST be initialized here, before the `if guild:`
            # block below — it used to live inside that block (as
            # `intercepted = None`), which meant in a DM (guild=None) it was
            # NEVER assigned at all. The later `if intercepted is None:`
            # checks that gate handle_media_request/handle_music_request are
            # OUTSIDE the `if guild:` block (they must run for DMs too), so
            # they'd hit a bare NameError on every single DM — silently
            # caught by discord.py's default on_message error handler,
            # which is why DMs never crashed visibly but also never reached
            # the media/music interceptors at all (the model would then
            # just hallucinate "I can't send images" in general chat,
            # having never actually been given the chance to try).
            intercepted = None

            if guild:
                server_hint = _looks_server_related(dispatch_text)

                LABEL_HANDLERS = {
                    "avatar": handle_server_avatar_query,
                    "banner": handle_server_banner_query,
                    "owner": handle_server_owner_query,
                    "verification": handle_server_verification_query,
                    "member_count": handle_member_count_query,
                    "age": handle_server_age_query,
                    "boost": handle_boost_status_query,
                    "locale": handle_locale_query,
                    "description": handle_server_description_query,
                    "all_metadata": handle_all_metadata_query,
                    "server_info": handle_server_info_query,
                    "channel_count": handle_channel_count_query,
                    "role_list": handle_role_list_query,
                    "role_query": handle_role_query,
                    "created": handle_created_query,
                    "user_created": handle_user_created_query,
                }

                # 🌸 STRATEGY COIN-FLIP — per user request, randomly pick
                # whether the AI classifier or the regex chain gets first
                # crack at this message, instead of always trusting the
                # classifier. Whichever one DOESN'T go first still runs as
                # the fallback if the first pick comes back empty — this
                # is purely about ORDER, nothing loses coverage. Only
                # applies when server_hint is True; if the message doesn't
                # look server-related at all, there's nothing to flip a
                # coin over and we skip straight to "none" like before.
                strategy = random.choice(["ai", "regex"]) if server_hint else "regex"

                async def _try_ai():
                    label = await self.bot.groq.classify_server_query(dispatch_text, message.author.name)
                    handler = LABEL_HANDLERS.get(label)
                    if handler is None:
                        return None
                    # 🌸 The AI classifier already decided what this
                    # message wants, often using phrasing/reply-context
                    # its handler's own regex can't parse on its own
                    # (message.content only, no reply context, rigid word
                    # order). Trust that classification for every label
                    # EXCEPT role_query — that handler's regex isn't just
                    # a gate, it also EXTRACTS the role name via a capture
                    # group, so skipping it would leave nothing to look
                    # up. Every other handler only ever reads guild_id/
                    # message.guild/message.mentions once past the gate,
                    # so skipping their gate is safe.
                    if label == "role_query":
                        return await handler(message, guild.id, shared)
                    return await handler(message, guild.id, shared, skip_pattern_check=True)

                async def _try_regex():
                    # 🌸 Most specific/rare patterns checked first (avatar, banner,
                    # owner, verification, boosts) so they never get shadowed by the more
                    # general "what is this server" catch-all below.
                    result = await handle_server_avatar_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_server_banner_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_server_owner_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_server_verification_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_member_count_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_server_age_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_boost_status_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_locale_query(message, guild.id, shared)
                    if result is None:
                        # 🌸 Specific-field asks (description, then "give me
                        # everything") must be tried BEFORE the generic
                        # server_info catch-all below — SERVER_INFO_PATTERN is
                        # broad enough to match "look at this server
                        # description" too, which would otherwise dump the
                        # whole curated overview instead of answering just the
                        # field that was actually asked for.
                        result = await handle_server_description_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_all_metadata_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_server_info_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_channel_count_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_role_list_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_role_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_created_query(message, guild.id, shared)
                    if result is None:
                        result = await handle_user_created_query(message, guild.id, shared)
                    return result

                if strategy == "ai":
                    intercepted = await _try_ai()
                    if intercepted is None:
                        # 🌸 AI picked nothing usable (label was "none", or the
                        # handler had nothing to answer with) — fall back to
                        # the regex chain so coverage never drops just because
                        # the coin flip landed on "ai" first.
                        intercepted = await _try_regex()
                else:
                    intercepted = await _try_regex()
                    if intercepted is None and server_hint:
                        # 🌸 Regex chain came up empty but the message still
                        # looks server-ish — give the AI classifier a shot
                        # before giving up entirely, same as the "ai" branch
                        # falling back to regex above.
                        intercepted = await _try_ai()

            # 🌸 DEDENTED OUT OF `if guild:` — this used to be nested inside
            # the guild-only block above, which meant handle_media_request/
            # handle_music_request (and the reply-sending logic below) never
            # ran at all in a DM: `if guild:` was False, so Python skipped
            # straight past all of this to the plain-chat path below, no
            # crash, no error, nothing — the model just answered from
            # general chat with no chance to actually search for media,
            # which is exactly why it kept insisting "I'm text-only" even
            # after the earlier `intercepted = None` / server_context fixes.
            # Those fixes stopped the crash, but this indentation bug was
            # still silently skipping the interceptors themselves in DMs.
            # `intercepted` is already guaranteed to exist here (initialized
            # above, before `if guild:`) whether or not that block ran.
            if intercepted is None:
                # 🌸 AI-classified media request — one Groq call decides
                # if this is "send me a pic/video/vector/cartoon of X",
                # then dispatches to Pexels (photo/video) or Pixabay
                # (vector/cartoon) and returns a discord.Embed with the
                # image set via set_image() — no raw link text visible,
                # unlike the old bare-URL auto-embed approach.
                intercepted = await handle_media_request(message, guild_id, shared)
            if intercepted is None:
                # 🌸 AI-classified MUSIC request — same zero-token-gate-
                # then-classify shape as everything else in this chain
                # (MUSIC_INTENT_PATTERN local pre-filter, then one Groq
                # call). Searches YouTube Music via ytmusicapi and
                # returns a discord.Embed with a cute AI summary + top
                # tracks, or None if this doesn't look like a music
                # request. Runs AFTER handle_media_request so an image
                # request never gets accidentally swallowed here first.
                intercepted = await handle_music_request(message, guild_id, shared)
            if intercepted:
                # 🌸 handle_media_request returns a plain discord.Embed.
                # handle_music_request returns a (discord.Embed,
                # tracks | None, query, summary | None) 4-tuple —
                # tracks/summary are None for the "couldn't find
                # anything" apology embed (nothing to paginate). When
                # tracks IS populated, buttons are PERSISTENT (survive
                # bot restarts — see MusicPaginatorView +
                # register_persistent_music_view in
                # groq_music_suggestion.py). custom_id is a FIXED
                # literal string ("music:prev"/"music:next"), not
                # message-id-encoded, so the view can be built and
                # attached in the SAME reply() call — no more
                # send-without-view-then-edit-in-a-view dance, and one
                # fewer Discord API round-trip per music result. `summary`
                # is threaded into save_music_interaction so ◀️/▶️ page
                # turns can keep showing the same AI blurb instead of it
                # only ever appearing on the first-sent page. Every
                # OTHER interceptor above returns a plain string reply.
                if isinstance(intercepted, tuple):
                    music_embed, music_tracks, music_query, music_summary = intercepted
                    if music_tracks:
                        music_view = MusicPaginatorView(owner_id=message.author.id)
                        _, music_url, video_url = _track_urls(music_tracks[0])
                        music_view.add_item(discord.ui.Button(label="YT Music", url=music_url, emoji="🎧"))
                        music_view.add_item(discord.ui.Button(label="YouTube", url=video_url, emoji="📲"))
                        sent = await message.reply(
                            embed=music_embed,
                            view=music_view,
                            mention_author=True, allowed_mentions=SAFE_REPLY_MENTIONS,
                        )
                        await save_music_interaction(
                            message_id=sent.id,
                            user_id=message.author.id,
                            guild_id=guild_id,
                            query=music_query,
                            filter_type="songs",
                            tracks=music_tracks,
                            track_index=0,
                            summary=music_summary,
                        )
                    else:
                        await message.reply(embed=music_embed, mention_author=True, allowed_mentions=SAFE_REPLY_MENTIONS)
                elif isinstance(intercepted, discord.Embed):
                    await message.reply(embed=intercepted, mention_author=True, allowed_mentions=SAFE_REPLY_MENTIONS)
                else:
                    await message.reply(intercepted, mention_author=True, allowed_mentions=SAFE_REPLY_MENTIONS)
                return

            # 🌸 Prompt no longer carries the full guild_context dump — the
            # compact, DB-backed guild summary is now injected inside
            # groq_ai.get_ai_response via shared.get_guild_context_summary,
            # so this prompt stays just the user's actual message.
            # 🌸 clean_content resolves mentions to readable @names/#channels
            # instead of raw <@123456789012345> snowflakes — without this the
            # AI sees unreadable numeric IDs and starts narrating that it
            # "can't see who that tag is pointing to" instead of just
            # replying to the actual message.
            # 🌸 Discord-native FORWARDED messages (the share/"Forward"
            # feature) arrive with clean_content EMPTY — the actual text
            # lives in message.message_snapshots instead. Without this,
            # a forward looks blank to Groq. Appended as its own labeled
            # block so it reads distinctly from whatever (if anything)
            # the forwarder typed alongside it.
            forwarded_text = self._extract_forwarded_text(message)

            # 🌸 IMAGE ATTACHMENTS — if the user posted image(s) alongside
            # their message, describe them via a SEPARATE Groq vision call
            # (extras/groq_attachments.py) and fold the description into
            # the text prompt as its own labeled block, same pattern as
            # forwarded_text above. This is intentionally NOT woven into
            # get_ai_response's own messages/content-blocks — the vision
            # call happens here, once, and the rest of the pipeline
            # (personality, history, [REACT:...], [DM_START]...) just
            # sees ordinary text either way, so nothing else needs to
            # know vision happened at all. Only attempted if there's at
            # least one actual image attachment — get_image_attachments
            # filters out non-image files, so a message with only a
            # regular file/video attachment skips this entirely with no
            # wasted Groq call.
            image_description = None
            if get_image_attachments(message):
                image_description = await describe_attachments(
                    self.bot.groq.client, message, user_text=message.clean_content,
                )

            prompt = (
                f"User: {message.author.name}\n"
                f"Message: {message.clean_content}\n"
            )
            if forwarded_text:
                prompt += f"[Forwarded message]: {forwarded_text}\n"
            if image_description:
                prompt += f"[Attached image]: {image_description}\n"

            # ✅ Check if user is asking for a reaction
            user_asking_for_react = bool(REACT_REQUEST_PATTERN.search(message.content))

            # ✅ Determine which emoji to suggest (if allowed to react)
            react_allowed = user_asking_for_react or random.random() < AUTO_REACT_CHANCE

            # 🌸 Same "regex gates/strengthens, AI decides content" shape
            # as react_allowed above — DM_REQUEST_PATTERN reliably catches
            # explicit "dm me"/"kirim ke dm" asks even when Groq itself
            # might not judge the content as "sensitive enough" to warrant
            # a [DM_START] tag on its own. Only meaningful in a guild
            # (get_ai_response's build_dm_directives already no-ops this
            # for DMs), but computing it unconditionally here is harmless.
            dm_requested = bool(DM_REQUEST_PATTERN.search(message.content))

            # 🌸 reply_to_message_id / reply_to_message_text were already
            # resolved earlier (before the server-query dispatch block
            # above) so that dispatch could see reply context too — no
            # need to fetch them again here, just reuse the same values.

            response = await self.bot.groq.get_ai_response(
                prompt,
                username=message.author.name,
                user_id=message.author.id,
                display_name=message.author.display_name,
                guild=message.guild,
                channel=message.channel,
                recent_react_emoji=self.recent_react_emoji.get(str(message.channel.id), []),
                react_allowed=react_allowed,  # ✅ ADDED: Pass react_allowed
                dm_requested=dm_requested,
                shared=shared,
                message_id=message.id,
                reply_to_message_id=reply_to_message_id,
                reply_to_message_text=reply_to_message_text,
            )

            if response:
                # ✅ Extract and apply emoji reactions BEFORE stripping tags
                await self._apply_emoji_reactions(message, response)

                # ✅ Strip [REACT:emoji] tags from response text so they don't show to users
                clean_response = REACT_TAG_PATTERN.sub("", response).strip()

                # 🌸 DM split-routing MUST run BEFORE the public send below —
                # it further strips [DM_START]...[DM_END] out of
                # clean_response (mutating what actually reaches the server
                # channel), fires the private DM off to the side, and
                # appends an AI-phrased, Discord-verified success/privacy-
                # blocked notice. See extras/groq_dm_instruct.route_dm_split
                # for the full contract; generate_dm_notice is what phrases
                # that notice in the bot's own voice.
                clean_response = await route_dm_split(
                    message, clean_response, self.bot.groq.generate_dm_notice,
                )

                try:
                    if clean_response:  # Only send if there's content after stripping
                        await message.reply(
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
