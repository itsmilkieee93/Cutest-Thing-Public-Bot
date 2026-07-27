import os
import sys
import re
import time
import random
import logging
import asyncio
import discord
from datetime import datetime
from groq import Groq
from groq import RateLimitError as GroqRateLimitError

from resources import shared

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
# Path is relative to CWD (bot root), matching this file's existing convention
# of using relative paths like "auth/groq_key" instead of __file__-based ones.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

from groq_instruct import (
    MODEL_POOL, SAFEGUARD_MODEL, SAFEGUARD_POLICY, OUTPUT_SAFEGUARD_POLICY,
    SAFEGUARD_BLOCK_REPLIES, REACT_EMOJI_POOL, REACT_INSTRUCTIONS_DISALLOWED,
    IDENTITY_INSTRUCTIONS, DM_CONTEXT_INSTRUCTIONS,
    REACT_TAG_PATTERN, REACT_REQUEST_PATTERN, EXPLICIT_EMOJI_PATTERN,
    REACT_EMOJI_POOL, RECENT_EMOJI_MEMORY, AUTO_REACT_CHANCE,
    _build_react_instructions, _strip_reasoning,
    _check_base64_for_severe_terms, _contains_severe_term
)

# 🌸 Dynamic personality — instructions are generated live from the bot's
# CURRENT per-guild nickname (set via /server-persona-set), instead of a
# static auth/personality.txt file. See personality.py for details.
from personality import get_personality_for_nickname

# 🌸 Dedicated file logger for Groq's x-ratelimit-* response headers — lets
# you track API usage/limits over time in logs/bot.log without cluttering
# stdout. The `if not groq_logger.handlers` guard keeps this safe to import
# more than once (e.g. via importlib.reload) without stacking duplicate
# handlers and writing every line twice.
os.makedirs("logs", exist_ok=True)
groq_logger = logging.getLogger("groq_ratelimit")
groq_logger.setLevel(logging.INFO)
if not groq_logger.handlers:
    _groq_log_handler = logging.FileHandler("logs/bot.log", encoding="utf-8")
    _groq_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    groq_logger.addHandler(_groq_log_handler)
    groq_logger.propagate = False


# 🌸 Full conversation transcript logger — every completed Groq call (both
# the user's prompt AND the model's reply) gets appended to log/groq_ai.log.
# Separate file/dir from logs/bot.log on purpose: that one is just
# ratelimit-header telemetry, this one is the actual chat content, so they
# don't get mixed together. Same reload-safe handler guard as groq_logger.
os.makedirs("log", exist_ok=True)
groq_ai_logger = logging.getLogger("groq_ai_transcript")
groq_ai_logger.setLevel(logging.INFO)
if not groq_ai_logger.handlers:
    _groq_ai_log_handler = logging.FileHandler("log/groq_ai.log", encoding="utf-8")
    _groq_ai_log_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    groq_ai_logger.addHandler(_groq_ai_log_handler)
    groq_ai_logger.propagate = False


class GroqService:
    """
    🌸 Powers the "priority" AI reply for @mentions/replies — a fast Groq
    (Llama 3.3 70B) response that takes precedence over the random/context
    message roulette, subject to a short per-channel cooldown (see
    EnchantedBot._groq_on_cooldown). Personality is now DYNAMIC — built
    live from the bot's current per-guild nickname (see personality.py)
    instead of a static personality.txt file, so changing the nickname
    via /server-persona-set changes the bot's AI voice too, no restart
    needed. self.personality_path / _load_file are kept only as a
    last-resort fallback if personality.py can't be imported for some
    reason.
    """
    def __init__(self, bot: "commands.Bot" = None):
        self.personality_path = "auth/personality.txt"

        # 🌸 Back-reference to the bot so 429 / success embeds can be fired
        # straight from get_ai_response via bot._broadcast_log_embed. May be
        # None (e.g. in a standalone script) — every call site guards for it.
        self.bot = bot

        self.api_keys          = list(key_config.GROQ_API_KEYS)
        self.current_key_index = 0

        if self.api_keys:
            self.client = Groq(api_key=self.api_keys[self.current_key_index])
        else:
            self.client = None
            print("❌ ERROR: No API keys found in key_config.GROQ_API_KEYS!")

        # 🌸 llama-3.3-70b-versatile was deprecated by Groq on 2026-06-17
        # (shutting down ~August 2026) — this fallback now points at
        # gpt-oss-120b instead. In normal operation model_to_use is picked
        # from MODEL_POOL via random.choice; this is just the last-resort
        # default if an explicit model_id is ever passed as None/empty.
        self.default_model = "openai/gpt-oss-120b"
        print(f"🌸 Groq priority-reply online with {len(self.api_keys)} hearts (API Keys)!")

    def _load_keys(self, path):
        try:
            with open(path, "r") as f:
                return [line.strip() for line in f.readlines() if line.strip()]
        except Exception as e:
            print(f"⚠️ Failed to load Groq keys: {e}")
            return []

    def _load_file(self, path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            return ""

    def rotate_key(self):
        if len(self.api_keys) <= 1:
            return
        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        self.client = Groq(api_key=self.api_keys[self.current_key_index])
        print(f"🔄 Swapping to Groq API Key #{self.current_key_index + 1}...")

    # 🌸 Square thumbnail shown in the corner of the log embeds.
    SUCCESS_THUMBNAIL_URL = "https://c.tenor.com/TcMXxO_U0dgAAAAC/tenor.gif"
    FAIL_THUMBNAIL_URL    = "https://c.tenor.com/Sn0nQ5dvHm4AAAAC/tenor.gif"

    def _extract_ratelimit_headers(self, headers) -> dict:
        """🌸 Pulls every x-ratelimit-* header off a Groq response into a
        plain dict, sorted for stable embed ordering. Returns {} if headers
        is falsy/missing so callers don't need to guard separately."""
        if not headers:
            return {}
        try:
            return {
                k: v for k, v in sorted(headers.items())
                if k.lower().startswith("x-ratelimit")
            }
        except Exception:
            return {}

    def _parse_groq_429(self, error: Exception) -> dict:
        """🌸 Pulls the useful bits out of a groq.RateLimitError's message
        body — org id, service tier, limit type (tokens/requests), limit,
        used, requested, and retry-after — via regex, since the SDK only
        exposes the raw message string. Any field that fails to match is
        just omitted from the embed rather than crashing. The upgrade/
        billing URL Groq includes is intentionally NOT captured/shown here.
        """
        text = str(error)
        fields = {}

        patterns = {
            "organization": r"organization `([^`]+)`",
            "service_tier": r"service tier `([^`]+)`",
            "limit_type":   r"on (tokens per day|requests per day|tokens per minute|requests per minute) \(?(TPD|RPD|TPM|RPM)?\)?",
            "limit":        r"Limit (\d+)",
            "used":         r"Used (\d+)",
            "requested":    r"Requested (\d+)",
            "retry_after":  r"try again in ([\w.]+?)\.?(?:\s|$)",
        }

        for key, pat in patterns.items():
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fields[key] = m.group(1)

        fields["model"] = getattr(error, "model", None)
        return fields

    def _build_rate_limit_embed(self, error: Exception, username: str, user_id: int, model_id: str) -> discord.Embed:
        """🌸 Rich embed for a Groq 429 — deliberately omits the
        console.groq.com billing/upgrade link Groq includes in the raw
        error text."""
        parsed = self._parse_groq_429(error)

        embed = discord.Embed(
            title="🚫 Groq Rate Limit Hit (429)",
            color=0xE74C3C,
            timestamp=datetime.now(),
        )
        embed.add_field(name="🧠 Model", value=f"`{model_id}`", inline=True)
        embed.add_field(name="🔑 Key", value=f"#{self.current_key_index + 1}", inline=True)
        embed.add_field(name="🏢 Org", value=f"`{parsed.get('organization', 'unknown')}`", inline=True)

        embed.add_field(name="🎚️ Service Tier", value=parsed.get("service_tier", "unknown"), inline=True)
        limit_type = parsed.get("limit_type", "unknown")
        embed.add_field(name="📊 Limit Type", value=limit_type, inline=True)
        embed.add_field(name="⏳ Retry After", value=parsed.get("retry_after", "unknown"), inline=True)

        if parsed.get("limit") and parsed.get("used"):
            embed.add_field(
                name="📈 Usage",
                value=f"{parsed['used']} / {parsed['limit']} (requested {parsed.get('requested', '?')})",
                inline=False,
            )

        embed.add_field(name="👤 User", value=f"{username} (`{user_id}`)", inline=False)
        embed.set_thumbnail(url=self.FAIL_THUMBNAIL_URL)
        embed.set_footer(text="Cutest Thing 🌸 | Groq rate-limit alert")
        return embed

    def _build_success_embed(self, username: str, user_id: int, guild, channel, model_id: str, headers=None) -> discord.Embed:
        """🌸 Green success-log embed sent after every completed Groq reply,
        with user/server/channel snowflake IDs for tracing, plus every raw
        x-ratelimit-* header Groq sent back for that request."""
        embed = discord.Embed(
            title="✅ Groq Reply Sent",
            color=0x2ECC71,
            timestamp=datetime.now(),
        )
        embed.add_field(name="🧠 Model", value=f"`{model_id}`", inline=True)
        embed.add_field(name="👤 User", value=f"{username} (`{user_id}`)", inline=False)
        embed.add_field(
            name="🌐 Server",
            value=f"{guild.name} (`{guild.id}`)" if guild else "DM (no server)",
            inline=True,
        )
        embed.add_field(
            name="💬 Channel",
            value=f"{getattr(channel, 'name', 'DM')} (`{channel.id}`)",
            inline=True,
        )

        # 🌸 Every x-ratelimit-* header Groq returned for this call —
        # Discord embed fields cap at 1024 chars, so this is chunked into
        # multiple fields if the header list ever grows long enough to need it.
        rl_headers = self._extract_ratelimit_headers(headers)
        if rl_headers:
            lines = [f"`{k}`: {v}" for k, v in rl_headers.items()]
            chunk = ""
            chunk_num = 0
            for line in lines:
                if len(chunk) + len(line) + 1 > 1024:
                    chunk_num += 1
                    embed.add_field(
                        name="📊 Rate Limit Headers" if chunk_num == 1 else "📊 Rate Limit Headers (cont.)",
                        value=chunk.strip(),
                        inline=False,
                    )
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                embed.add_field(
                    name="📊 Rate Limit Headers" if chunk_num == 0 else "📊 Rate Limit Headers (cont.)",
                    value=chunk.strip(),
                    inline=False,
                )

        embed.set_thumbnail(url=self.SUCCESS_THUMBNAIL_URL)
        embed.set_footer(text="Cutest Thing 🌸 | Groq activity log")
        return embed

    def _log_ai_transcript(self, username: str, user_id: int, model_id: str,
                            guild, channel, prompt: str, reply: str):
        """🌸 Appends one full prompt+response pair to log/groq_ai.log.
        Each entry is wrapped in a '----' separator line so entries are
        easy to visually scan/split on when reading the file, and
        multi-line prompts/replies are indented so they stay grouped
        under their own entry instead of blurring into the next one.
        Best-effort — a logging failure never breaks the actual reply."""
        try:
            guild_part = f"{guild.name} ({guild.id})" if guild else "DM"
            channel_part = f"{getattr(channel, 'name', 'DM')} ({getattr(channel, 'id', 'n/a')})"

            def _indent(text: str) -> str:
                return "\n".join(f"    {line}" for line in text.splitlines()) or "    (empty)"

            separator = "-" * 60
            entry = (
                f"{separator}\n"
                f"USER    : {username} ({user_id})\n"
                f"MODEL   : {model_id}\n"
                f"SERVER  : {guild_part}\n"
                f"CHANNEL : {channel_part}\n"
                f"PROMPT:\n{_indent(prompt)}\n"
                f"RESPONSE:\n{_indent(reply)}\n"
                f"{separator}"
            )
            groq_ai_logger.info(entry)
        except Exception as e:
            print(f"⚠️ Failed to write groq_ai.log entry: {e}")

    def _log_rate_limits(self, headers, username: str, model_id: str):
        """🌸 Pulls every x-ratelimit-* header off a Groq response (requests/
        tokens limit, remaining, reset — whatever Groq is currently sending)
        and appends a line to logs/bot.log via groq_logger. Best-effort: any
        failure here just prints a warning and never breaks the actual reply."""
        try:
            limit_headers = {
                k: v for k, v in headers.items()
                if k.lower().startswith("x-ratelimit")
            }
            if limit_headers:
                summary = ", ".join(f"{k}={v}" for k, v in sorted(limit_headers.items()))
                groq_logger.info(f"[{username} | {model_id} | Key #{self.current_key_index + 1}] {summary}")
        except Exception as e:
            print(f"⚠️ Rate-limit log error: {e}")

    async def check_safety(self, prompt: str, username: str) -> bool:
        """
        🌸 Runs the incoming prompt through openai/gpt-oss-safeguard-20b
        (Groq's policy-based safety classifier) BEFORE it ever reaches the
        main chat model. Returns True if the message is safe to respond to,
        False if it should be blocked.

        FAILS OPEN: if the safeguard call itself errors out (network blip,
        rate limit, bad parse, etc.) this returns True so a Groq hiccup
        never silently mutes the bot for everyone — it only blocks on an
        actual UNSAFE verdict.

        Costs one extra Groq request per priority reply (doubles API usage
        against the free-tier limits) — worth knowing given past token-burn
        issues, so keep an eye on rate-limit logs after enabling this.
        """
        # 🌸 Layer 0: Check for base64-encoded harmful content (e.g.,
        # 'bmlnZ2E=' → 'nigga'). Catches encoding tricks before even
        # hitting the safeguard model.
        is_b64_flagged, decoded_b64 = _check_base64_for_severe_terms(prompt)
        if is_b64_flagged:
            print(f"🛡️ Base64 decoder caught harmful content from {username}: {decoded_b64!r} (was {prompt!r})")
            return False

        if not self.client:
            return True

        def _call():
            return self.client.chat.completions.create(
                model=SAFEGUARD_MODEL,
                messages=[
                    {"role": "system", "content": SAFEGUARD_POLICY},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=10,
            )

        try:
            response = await asyncio.to_thread(_call)
            verdict = (response.choices[0].message.content or "").strip().upper()
            is_safe = not verdict.startswith("UNSAFE")
            if not is_safe:
                print(f"🛡️ Safeguard blocked a message from {username}: {prompt[:80]!r}")
            return is_safe
        except Exception as e:
            print(f"⚠️ Safeguard check error (failing open): {e}")
            return True

    async def check_output_safety(self, reply: str, username: str) -> bool:
        """
        🌸 Runs the CHAT MODEL'S OWN REPLY through the safeguard model,
        catching cases where an innocent-looking prompt (e.g. a cipher/
        decode request) tricked the model into generating disallowed
        content that check_safety's prompt-only screening would never see.

        Returns True if the reply is safe to send, False if it should be
        replaced with a refusal.

        Three layers, cheapest first:
          1. Instant local regex/normalization check for severe terms —
             catches slurs even through leetspeak/spacing obfuscation with
             zero extra latency or API cost.
          2. Base64 decoder check — catches if reply contains encoded
             harmful content (e.g., a model tricked into generating 'bmlnZ2E=').
          3. Groq safeguard model call for everything else — same
             fail-open behavior as check_safety (a Groq hiccup here should
             never mean the bot goes silent), but layers 1-2 already cover
             the worst case even if this call fails.
        """
        # Layer 1: Direct severe term check
        if _contains_severe_term(reply):
            print(f"🛡️ Local filter blocked a reply meant for {username}: {reply[:80]!r}")
            return False

        # Layer 2: Base64-encoded content check
        is_b64_flagged, decoded_b64 = _check_base64_for_severe_terms(reply)
        if is_b64_flagged:
            print(f"🛡️ Base64 check caught harmful output from model for {username}: {decoded_b64!r}")
            return False

        if not self.client:
            return True

        def _call():
            return self.client.chat.completions.create(
                model=SAFEGUARD_MODEL,
                messages=[
                    {"role": "system", "content": OUTPUT_SAFEGUARD_POLICY},
                    {"role": "user", "content": reply},
                ],
                temperature=0,
                max_tokens=10,
            )

        try:
            response = await asyncio.to_thread(_call)
            verdict = (response.choices[0].message.content or "").strip().upper()
            is_safe = not verdict.startswith("UNSAFE")
            if not is_safe:
                print(f"🛡️ Output safeguard blocked a reply meant for {username}: {reply[:80]!r}")
            return is_safe
        except Exception as e:
            print(f"⚠️ Output safeguard check error (failing open): {e}")
            return True

    async def get_ai_response(self, prompt: str, username: str, user_id: int, display_name: str = None, model_id: str = None, react_allowed: bool = False, guild=None, channel=None, recent_react_emoji: list[str] = None, shared=None) -> str | None:
        """
        Runs the (blocking) Groq SDK call in a worker thread so it never
        stalls the bot's event loop. Loads this user's saved chat history
        from groq/memory/{guild_id}/memory.db (via shared.load_groq_memory)
        and includes it for multi-turn context, then appends this exchange
        and saves it back — trimmed to the last 200 turns so the DB row/
        prompt don't grow unbounded. Memory is now PER-GUILD: one SQLite
        file per guild (DMs use guild_id=0), so what someone says in
        Server A no longer bleeds into Server B's context. Within each
        guild's file, memory is SHARED across every model in MODEL_POOL
        (one row per user_id within that guild, not per user_id+model),
        so the random per-turn model pick below no longer resets context
        when it happens to land on a different model than last time. Also
        injects the sender's real Discord username/display name into the
        system prompt every call (see IDENTITY_INSTRUCTIONS) so the AI
        knows who it's talking to without them ever having to say it
        themselves. Returns None on total failure so the caller can fall
        back to the random/context roulette instead.

        `react_allowed` gates whether Groq is told it may emit a
        [REACT:...] tag this turn at all — see REACT_REQUEST_PATTERN /
        AUTO_REACT_CHANCE where the caller decides this.
        """
        if not self.client:
            return None

        if not await self.check_safety(prompt, username):
            return random.choice(SAFEGUARD_BLOCK_REPLIES)

        # 🌸 Random model pick per turn (unless caller pinned model_id) —
        # see MODEL_POOL up top for what's in rotation.
        model_to_use     = model_id or random.choice(MODEL_POOL)

        # 🌸 Personality now follows the bot's CURRENT per-guild nickname
        # (set via /server-persona-set) instead of a static file — read
        # fresh every call so a nickname change takes effect on the very
        # next reply, with zero code changes needed for new personas.
        # guild.me is None in a DM, so nickname falls back to the default
        # inside get_personality_for_nickname().
        nickname         = guild.me.nick if guild and guild.me else None
        try:
            personality  = get_personality_for_nickname(nickname)
        except Exception as e:
            # 🌸 Last-resort fallback if personality.py errors out for
            # any reason — keeps the bot answering instead of crashing.
            print(f"⚠️ Dynamic personality load failed, falling back to file: {e}")
            personality  = self._load_file(self.personality_path)
        identity         = IDENTITY_INSTRUCTIONS.format(
            display_name=display_name or username,
            username=username,
        )
        if react_allowed:
            # 🌸 Pick a random example emoji that ISN'T one of this
            # channel's recent auto-reacts, so the example itself nudges
            # Groq away from repeating — then also spell out the avoid-list
            # explicitly (see _build_react_instructions).
            avoid_set   = set(recent_react_emoji or [])
            example_pool = [e for e in REACT_EMOJI_POOL if e not in avoid_set] or REACT_EMOJI_POOL
            example_emoji = random.choice(example_pool)
            react_directives = _build_react_instructions(example_emoji, recent_react_emoji)
        else:
            react_directives = REACT_INSTRUCTIONS_DISALLOWED

        # 🌸 Server context — pulled from the v10 REST API and cached (see
        # EnchantedBot.get_server_context_text) so Groq always knows what
        # server it's replying in, same idea as IDENTITY_INSTRUCTIONS but
        # for "where" instead of "who". Falls back to DM_CONTEXT_INSTRUCTIONS
        # when guild is None, or "" if there's no bot back-reference at all
        # (e.g. running this service standalone outside EnchantedBot).
        if self.bot:
            server_context = self.bot.get_server_context_text(guild)
        else:
            server_context = DM_CONTEXT_INSTRUCTIONS if guild is None else ""

        # 🌸 Pull compact guild info straight from the per-guild SQLite
        # cache (metadata.db/roles.db/channels.db) instead of dumping the
        # full guild JSON into the prompt. shared.get_guild_context_summary
        # already does the GROUP BY queries + formatting — just await it.
        guild_summary = ""
        if guild:
            try:
                guild_summary = await shared.get_guild_context_summary(guild.id)
            except Exception as e:
                print(f"❌ guild_summary fetch error ({guild.id}): {e}")
                guild_summary = ""
        
        # 🌸 Explicit "ignore old data" instruction — when answering questions about
        # channels/server info, use ONLY the current server data below, not channel
        # names from old conversations in OTHER servers (which may be in chat history).
        server_override = (
            "⚠️ IMPORTANT: When answering questions about channels, server info, or what exists here, "
            "use ONLY the current server information below. Ignore any channel names from past "
            "conversations — they may be from a different server. When listing channels, format them "
            "as a comma-separated list on a single line (e.g., '#general, #announcements, #off-topic') "
            "or in a compact table, NOT as bullet points. The CURRENT server is:"
        ) if guild and guild_summary else ""
        
        personality      = f"{personality}\n\n{react_directives}\n\n{identity}\n\n{server_context}\n\n{server_override}\n{guild_summary}".strip()
        # 🌸 user_id here is always the Discord snowflake ID of whoever
        # actually pinged/replied to the bot (passed straight through from
        # message.author.id in on_message / handle_mention_reaction), so
        # memory always follows the right person even if their Discord
        # username changes later. Memory is now scoped PER-GUILD as well
        # as per-user — guild.id (or 0 for DMs) keeps what someone says
        # in one server from leaking into another server's context, while
        # still being shared across every model in MODEL_POOL within that
        # guild (doesn't matter which model answered last turn).
        guild_id         = guild.id if guild else 0
        history          = await shared.load_groq_memory(user_id, guild_id)

        # 🌸 SLICING: only ship a random-sized recent window (from THIS
        # user_id's shared history) to Groq to keep input tokens small and
        # vary the model's context a bit turn to turn. `history` itself
        # stays FULL (up to 200 turns) — it's what gets appended to and
        # saved back to bot_history.db below, so nothing is lost from
        # long-term memory. `recent_history` is just the window actually
        # sent to the API this call: random.randint(0, 5) * 2 gives an
        # even count of 0, 2, 4, 6, 8, or 10 messages.
        recent_turn_count = random.randint(1, 6) * 2
        recent_history    = history[-recent_turn_count:] if recent_turn_count else []

        def _call():
            # 1. Buat parameter dasar yang selalu digunakan semua model
            kwargs = {
                "model": model_to_use,
                "messages": [
                    {"role": "system", "content": personality},
                    *recent_history,
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": random.randint(100, 1000),
            }

            # 2. Atur parameter reasoning secara dinamis sesuai tipe model
            model_lower = model_to_use.lower()

            if "qwen" in model_lower:
                # Qwen menolak reasoning_format, gunakan effort: none untuk mematikan thinking
                kwargs["reasoning_effort"] = "none"
            elif "llama" in model_lower:
                # Llama standar tidak mendukung parameter reasoning sama sekali.
                # Kita biarkan tanpa reasoning_format atau reasoning_effort agar tidak error 400.
                pass
            else:
                # GPT-OSS / DeepSeek menerima reasoning_format
                kwargs["reasoning_format"] = "hidden"
                # Paksa gpt-oss berpikir seminimal mungkin agar cepat
                if "gpt-oss" in model_lower:
                    kwargs["reasoning_effort"] = "low"                    

            try:
                # 3. Jalankan request dengan mendekompresi (unpack) kwargs
                return self.client.chat.completions.with_raw_response.create(**kwargs)

            except GroqRateLimitError:
                # 🌸 Re-raise so it's caught by the outer try/except below
                raise

        for attempt in range(max(len(self.api_keys), 1)):
            try:
                raw_response = await asyncio.to_thread(_call)
                self._log_rate_limits(raw_response.headers, username, model_to_use)

                completion = raw_response.parse()
                reply = _strip_reasoning(completion.choices[0].message.content)

                # 🌸 OUTPUT-SIDE safety gate — catches cases where an
                # innocent-looking prompt (cipher/decode tricks, etc.)
                # slipped past check_safety's prompt-only screening and got
                # the model to actually generate disallowed content. If
                # flagged, swap in a refusal BEFORE it's saved to memory,
                # written to the transcript log, or sent to Discord.
                if not await self.check_output_safety(reply, username):
                    reply = random.choice(SAFEGUARD_BLOCK_REPLIES)
                    self._log_ai_transcript(
                        username, user_id, model_to_use, guild, channel,
                        prompt, "[BLOCKED BY OUTPUT SAFEGUARD — reply withheld]",
                    )
                else:
                    # 🌸 Full prompt+response transcript → log/groq_ai.log
                    self._log_ai_transcript(username, user_id, model_to_use, guild, channel, prompt, reply)

                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": reply})
                await shared.save_groq_memory(model_to_use, username, user_id, history[-200:], guild_id)

                # 🌸 Success log — fired for EVERY completed Groq reply, not
                # just errors. Best-effort: never let a logging hiccup take
                # down the actual reply.
                if self.bot and channel is not None:
                    try:
                        success_embed = self._build_success_embed(
                            username, user_id, guild, channel, model_to_use,
                            headers=raw_response.headers,
                        )
                        asyncio.create_task(self.bot._broadcast_log_embed(success_embed))
                    except Exception as log_err:
                        print(f"⚠️ Success log embed error: {log_err}")

                return reply
            except GroqRateLimitError as e:
                # 🌸 The SDK's own dedicated 429 exception — this is the
                # real, unambiguous signal that a key is actually rate
                # limited (see groq-python docs: groq.RateLimitError is
                # raised specifically for 429s, distinct from any other
                # APIStatusError). No guessing from error text needed.
                err_headers = getattr(getattr(e, "response", None), "headers", None)
                if err_headers:
                    self._log_rate_limits(err_headers, username, model_to_use)

                print(f"⚠️ Groq RateLimitError (status_code={getattr(e, 'status_code', '?')}): {e}")
                print(f"   Rotating from Key #{self.current_key_index + 1}...")

                # 🌸 Rich 429 embed → every configured log channel. Fired
                # BEFORE rotate_key() so "Key #" in the embed reflects the
                # key that actually got limited, not the new one.
                if self.bot:
                    try:
                        rl_embed = self._build_rate_limit_embed(e, username, user_id, model_to_use)
                        asyncio.create_task(self.bot._broadcast_log_embed(rl_embed))
                    except Exception as log_err:
                        print(f"⚠️ Rate-limit embed error: {log_err}")

                self.rotate_key()
                continue
            except Exception as e:
                # 🌸 BUGFIX: this used to check `"rate" in str(e).lower()`,
                # which false-positives on all kinds of unrelated errors —
                # "generate", "moderate", "separate", "duplicate",
                # "collaborate", etc. all contain "rate" as a substring, so
                # ANY unrelated exception got misclassified as a 429 and
                # burned through every key rotating for no reason (with the
                # logged x-ratelimit-remaining-tokens often still showing
                # the FULL limit, proving it wasn't a real rate limit at
                # all). Only groq.RateLimitError above should ever trigger
                # a rotation now — everything else (parse errors, network
                # issues, etc.) is logged and returns None so the caller
                # falls back to the random/context roulette instead.
                err_headers = getattr(getattr(e, "response", None), "headers", None)
                if err_headers:
                    self._log_rate_limits(err_headers, username, model_to_use)

                print(f"⚠️ Groq error ({type(e).__name__}): {e}")
                return None

        return None
