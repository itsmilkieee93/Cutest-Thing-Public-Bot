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
from groq import APIStatusError as GroqAPIStatusError

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
    _build_react_instructions, _strip_reasoning, _build_owner_status,
    _check_base64_for_severe_terms, _contains_severe_term,
    CLASSIFIER_MODEL, SERVER_QUERY_LABELS, SERVER_QUERY_CLASSIFIER_POLICY,
    _wants_web_search, _search_domains_for_prompt, SEARCH_INTENT_CLASSIFIER_POLICY,
)
from extras.groq_dm_instruct import build_dm_directives

# 🌸 Dynamic personality — instructions are generated live from the bot's
# CURRENT per-guild nickname (set via /server-persona-set), instead of a
# static auth/personality.txt file. See personality.py for details.
from personality import get_personality_for_nickname, load_personality

# 🌸 Exa web search — PRIMARY search path now, ahead of groq/compound-mini
# (see GroqService.__init__ and the _wants_web_search branch in
# get_ai_response below). Wrapped in try/except so a missing exa_py
# package or unset EXA_API_KEY degrades gracefully to the existing
# compound/browser_search chain instead of crashing bot startup —
# ExaSearchService is None in that case and every call site checks for it.
try:
    from groq_exa_search import ExaSearchService
except Exception as _exa_import_err:
    ExaSearchService = None
    print(f"⚠️ groq_exa_search import failed ({_exa_import_err}) — Exa search disabled, using compound only.")

# 🌸 Dedicated file logger for Groq's x-ratelimit-* response headers — lets
# you track API usage/limits over time in logs/bot.log without cluttering
# stdout. The `if not groq_logger.handlers` guard keeps this safe to import
# more than once (e.g. via importlib.reload) without stacking duplicate
# handlers and writing every line twice.
#
# 🌸 NOT relayed to stdout on purpose — the rich version of this data
# (with the tenor thumbnail) already goes to Discord as a real embed via
# _build_success_embed / _build_rate_limit_embed. Relaying groq_logger
# too would've meant every rate-limit event showed up TWICE: once as the
# nice thumbnail embed, once as a plain-text log_webhook embed.
os.makedirs("logs", exist_ok=True)
groq_logger = logging.getLogger("groq_ratelimit")
groq_logger.setLevel(logging.INFO)
if not groq_logger.handlers:
    _groq_log_handler = logging.FileHandler("logs/bot.log", encoding="utf-8")
    _groq_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    groq_logger.addHandler(_groq_log_handler)
    groq_logger.propagate = False

from log_webhook import add_stdout_relay


# 🌸 Full conversation transcript logger — every completed Groq call (both
# the user's prompt AND the model's reply) gets appended to log/groq_ai.log.
# Separate file/dir from logs/bot.log on purpose: that one is just
# ratelimit-header telemetry, this one is the actual chat content, so they
# don't get mixed together. File-only on purpose — kept exactly as before.
os.makedirs("log", exist_ok=True)
groq_ai_logger = logging.getLogger("groq_ai_transcript")
groq_ai_logger.setLevel(logging.INFO)
if not groq_ai_logger.handlers:
    _groq_ai_log_handler = logging.FileHandler("log/groq_ai.log", encoding="utf-8")
    _groq_ai_log_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    groq_ai_logger.addHandler(_groq_ai_log_handler)
    groq_ai_logger.propagate = False

# 🌸 Separate, relay-ONLY logger (no file handler) for the flattened,
# single-line version of each transcript entry — see _log_ai_transcript.
# Kept apart from groq_ai_logger so log/groq_ai.log's format never changes
# and this one never accidentally gets a file handler of its own.
#
# ⚠️ Streams full user prompts + full AI replies to Discord #status via
# log_webhook. Fine for an owner-only/private status channel — worth
# knowing if anyone else ever gets access to it.
_groq_ai_relay_logger = logging.getLogger("groq_ai_transcript_relay")
_groq_ai_relay_logger.setLevel(logging.INFO)
_groq_ai_relay_logger.propagate = False
add_stdout_relay(_groq_ai_relay_logger, prefix="GroqAI")


def _first_notice_line(text: str, must_contain: str | None = None) -> str:
    """🌸 Defensive backstop for generate_dm_notice — collapses a
    completion down to just ONE paragraph, even if the model ignored
    the "exactly one sentence" instruction and tacked on a second,
    unrelated greeting/thought (the actual bug seen in production: two
    full sentences stacked in one reply, e.g. a DM confirmation
    immediately followed by an unrelated "hope you're vibing!" line).

    Splits on blank lines first (paragraph-level — the observed failure
    mode was always two DOUBLE-newline-separated sentences, i.e. the
    model treating them as two separate "messages"), then falls back to
    single newlines if there's no blank-line split at all.

    If `must_contain` is given (the Forbidden-outcome mention), prefers
    whichever paragraph actually contains it — so truncating never
    accidentally throws away the one line with the required @-mention
    in favor of an earlier, mention-less paragraph.
    """
    text = text.strip()
    if not text:
        return text

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) <= 1:
        # 🌸 No blank-line split — try single newlines as a weaker signal
        # of "the model stacked separate thoughts on separate lines".
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if len(paragraphs) <= 1:
        return text

    if must_contain:
        for p in paragraphs:
            if must_contain in p:
                return p

    return paragraphs[0]


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

        # 🌸 Exa search — see get_ai_response's _wants_web_search branch.
        # None when the exa_py package isn't installed (import already
        # failed and printed above) so every call site just checks
        # `if self.exa` instead of needing its own try/except.
        self.exa = ExaSearchService() if ExaSearchService else None

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

            # 🌸 The file entry above is deliberately multi-line for
            # readability in log/groq_ai.log — but log_webhook's Tee
            # queues per PHYSICAL LINE, so relaying that same multi-line
            # block to stdout would fragment one conversation across many
            # separate queued lines, getting interleaved with unrelated
            # log output from other modules in the same batch window.
            # Relay a single collapsed line instead, truncated so one
            # entry can't dominate a whole Discord batch on its own.
            def _flatten(text: str, limit: int = 300) -> str:
                flat = " ".join(text.split())
                return flat if len(flat) <= limit else flat[: limit - 3] + "..."

            relay_line = (
                f"{username} ({user_id}) @ {guild_part} / {channel_part} "
                f"[{model_id}] PROMPT: {_flatten(prompt)} "
                f"| RESPONSE: {_flatten(reply)}"
            )
            _groq_ai_relay_logger.info(relay_line)
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
                # 🌸 gpt-oss-safeguard-20b is documented (see groq_instruct.py)
                # as a "safety REASONING model" — it spends tokens on hidden
                # chain-of-thought before writing SAFE/UNSAFE, so max_tokens=10
                # with no reasoning_effort almost certainly burned the whole
                # budget on invisible reasoning and returned empty content
                # every time. That's the WORST version of this bug found so
                # far: check_safety fails OPEN (returns True) on empty/failed
                # content, meaning this safety gate has likely been silently
                # rubber-stamping everything as safe instead of actually
                # classifying it. Same root cause as the classifier bugs
                # already fixed in groq_pexels.py, groq_exa_search.py, and
                # classify_server_query/classify_search_intent above —
                # reasoning_effort="low" + real max_tokens headroom fixes it.
                reasoning_effort="low",
                max_tokens=150,
            )

        try:
            response = await asyncio.to_thread(_call)
            verdict = (response.choices[0].message.content or "").strip().upper()
            if not verdict:
                print(f"🛡️⚠️ Safeguard returned EMPTY content for {username} — check reasoning_effort/max_tokens (failing open)")
                return True
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

        # 🌸 Defensive truncation — this is the one safeguard call that
        # screens the CHAT MODEL'S full reply (not a short user prompt), so
        # an unusually long reply can eat into the reasoning/output budget
        # and starve the verdict token even with max_tokens headroom below.
        # Slurs/harmful content that would trigger UNSAFE virtually always
        # show up early; capping the input here doesn't weaken the check.
        reply_for_check = reply[:2000]

        def _call():
            return self.client.chat.completions.create(
                model=SAFEGUARD_MODEL,
                messages=[
                    {"role": "system", "content": OUTPUT_SAFEGUARD_POLICY},
                    {"role": "user", "content": reply_for_check},
                ],
                temperature=0,
                # 🌸 Same reasoning-model fix as check_safety above.
                reasoning_effort="low",
                max_tokens=300,
            )

        try:
            response = await asyncio.to_thread(_call)
            verdict = (response.choices[0].message.content or "").strip().upper()

            # 🌸 One retry before failing open — this gate gets a single
            # transient empty-content response more often than it should,
            # and failing open on the FIRST miss defeats the point of an
            # output safety check. A second attempt costs one extra call
            # only in the rare empty case, not on the normal path.
            if not verdict:
                print(f"🛡️⚠️ Output safeguard returned EMPTY content for {username} on first attempt — retrying once")
                response = await asyncio.to_thread(_call)
                verdict = (response.choices[0].message.content or "").strip().upper()

            if not verdict:
                print(f"🛡️⚠️ Output safeguard returned EMPTY content for {username} on retry too — check reasoning_effort/max_tokens (failing open)")
                return True
            is_safe = not verdict.startswith("UNSAFE")
            if not is_safe:
                print(f"🛡️ Output safeguard blocked a reply meant for {username}: {reply[:80]!r}")
            return is_safe
        except Exception as e:
            print(f"⚠️ Output safeguard check error (failing open): {e}")
            return True

    async def _generate_safeguard_decline(self, username: str, display_name: str = None, guild=None) -> str:
        """
        🌸 LIVE decline, not a template. Previously both the input-side
        (check_safety) and output-side (check_output_safety) blocks fell
        back to `random.choice(SAFEGUARD_BLOCK_REPLIES)` — the same 4
        canned lines on repeat forever, which reads as an obvious
        copy-pasted bot response instead of the bot actually "talking".
        This makes one small, cheap Groq call so the decline is generated
        fresh every time, in the bot's real personality, addressed to the
        actual person — same idea as every other reply, just short and
        firm. Kept deliberately separate from get_ai_response (no memory
        load/save, no search, no history) so a blocked message costs as
        little as possible while still sounding alive.

        🌸 Now pulls the bot's CURRENT per-guild personality/nickname from
        personality.py (same source get_ai_response's main system prompt
        uses) instead of a separate hardcoded "cute gen-z bot" blurb, so a
        decline matches whatever nickname/vibe /server-persona-set gave
        that guild instead of sounding like a generic stand-in bot.

        🌸 System prompt is JUST personality_instructions now — no extra
        decline-specific rules layered on top. personality.py already
        says "no corporate refusals, no lecture, just say no casually",
        so repeating/rephrasing that here only fought with it and made
        replies drift toward generic filler ("nope, not gonna happen")
        or over-explained mini-lectures. The user turn is a minimal
        bracketed scene-setter, not an instruction block, so the model's
        own personality does all the talking.

        FAILS CLOSED to the static SAFEGUARD_BLOCK_REPLIES list if this
        call itself errors or comes back empty — a decline template is a
        perfectly safe fallback (never blocks safety), it just shouldn't
        be the DEFAULT path anymore.
        """
        if not self.client:
            return random.choice(SAFEGUARD_BLOCK_REPLIES)

        # 🌸 Resolve live personality instructions the same way the main
        # chat path does: per-guild nickname lookup if we have a guild,
        # else the global default template (e.g. DMs, or lookup failure).
        try:
            if guild is not None and self.bot is not None:
                personality_instructions = await load_personality(self.bot, guild.id)
            else:
                personality_instructions = get_personality_for_nickname(None)
        except Exception as e:
            print(f"⚠️ Safeguard decline personality load failed (using default): {e}")
            personality_instructions = get_personality_for_nickname(None)

        def _call():
            return self.client.chat.completions.create(
                model=self.default_model,
                messages=[
                    {
                        "role": "system",
                        "content": personality_instructions,
                    },
                    {
                        "role": "user",
                        "content": (
                            f"[{display_name or username} (@{username}) sent something "
                            "you're declining to engage with. Reply in character.]"
                        ),
                    },
                ],
                temperature=1.05,
                reasoning_effort="low",
                max_tokens=60,
            )

        try:
            response = await asyncio.to_thread(_call)
            decline = _strip_reasoning(response.choices[0].message.content or "").strip()
            if not decline:
                print(f"🛡️⚠️ Safeguard decline generation returned EMPTY for {username} — using static fallback")
                return random.choice(SAFEGUARD_BLOCK_REPLIES)
            return decline
        except Exception as e:
            print(f"⚠️ Safeguard decline generation failed (using static fallback): {e}")
            return random.choice(SAFEGUARD_BLOCK_REPLIES)

    async def generate_dm_notice(self, message: discord.Message, outcome: str) -> str:
        """
        🌸 Generates the PUBLIC line reporting what actually happened
        with a DM split-route delivery — see extras/groq_dm_instruct.py's
        route_dm_split, which calls this as its `ai_notice` callback.

        outcome is "sent" (user.send succeeded) or "forbidden" (DMs
        closed / delivery failed) — the REAL Discord-verified result,
        never the model's own guess. This exists specifically so the
        public message never lies about delivery status the way the
        model's own inline confirmation could (it doesn't know yet
        whether the DM will land when it writes that text) — same
        "small, cheap, in-character, no memory load" call as
        _generate_safeguard_decline, just reporting a different fact.

        FAILS to a short static line per outcome if the client is
        missing or the call errors/comes back empty — reporting SOME
        accurate status always beats reporting nothing, and reporting
        nothing would look identical to the feature silently doing
        nothing at all.
        """
        static_fallback = {
            "sent": "sent it to your DMs! 🌸💌",
            "forbidden": (
                f"{message.author.mention} I tried to DM you that but your "
                f"privacy settings are blocking me! 🌸 Check **Privacy "
                f"Settings > \"Allow direct messages from server members\"** "
                f"for this server and I'll try again~"
            ),
        }.get(outcome, "")

        if not self.client:
            return static_fallback

        try:
            if message.guild is not None:
                personality_instructions = await load_personality(self.bot, message.guild.id)
            else:
                personality_instructions = get_personality_for_nickname(None)
        except Exception as e:
            print(f"⚠️ DM notice personality load failed (using default): {e}")
            personality_instructions = get_personality_for_nickname(None)

        display_name = message.author.display_name
        username = message.author.name

        if outcome == "sent":
            scene = (
                f"[You just successfully sent {display_name} (@{username}) a "
                "DM with something they asked for / something private. Tell "
                "them, in the SERVER channel, in character, that it's in "
                "their DMs now. EXACTLY ONE short sentence — no greeting, no "
                "second sentence, no extra thought tacked on after it. Just "
                "the one confirmation line and stop.]"
            )
        else:
            scene = (
                f"[You just TRIED to DM {display_name} (@{username}) "
                "something, but Discord blocked it — their privacy settings "
                "don't allow DMs from server members. Tell them this, in "
                "character, in the SERVER channel, and that they should "
                "check their Privacy Settings ('Allow direct messages from "
                f"server members') for this server so you can try again. "
                f"You MUST @-mention them by writing exactly {message.author.mention} "
                "somewhere in your reply. EXACTLY ONE short sentence covering "
                "both the problem and the fix — no greeting, no second "
                "sentence, no extra thought tacked on after it.]"
            )

        def _call():
            return self.client.chat.completions.create(
                model=self.default_model,
                messages=[
                    {"role": "system", "content": personality_instructions},
                    {"role": "user", "content": scene},
                ],
                temperature=0.9,
                reasoning_effort="low",
                max_tokens=40,
            )

        try:
            response = await asyncio.to_thread(_call)
            notice = _strip_reasoning(response.choices[0].message.content or "").strip()
            if not notice:
                print(f"⚠️ DM notice generation returned EMPTY for {username} (outcome={outcome}) — using static fallback")
                return static_fallback

            # 🌸 DEFENSIVE BACKSTOP — the scene prompt says "EXACTLY ONE
            # sentence", but low-effort/high-temp completions sometimes
            # still tack on a second unrelated greeting/thought anyway
            # (e.g. "sent it! 🌸" + "\n\nYo! Hope you're vibing! ✨"). Rather
            # than trust the model's restraint alone, collapse to just the
            # single paragraph/sentence that actually matters, so a stray
            # extra line can never reach the channel.
            notice = _first_notice_line(notice, must_contain=message.author.mention if outcome == "forbidden" else None)

            # 🌸 Forbidden notices MUST actually ping the user (so they
            # see it/know to check settings) — if the model dropped the
            # mention despite being told to include it (even after the
            # truncation above tried to keep the mention-bearing line),
            # fall back to the guaranteed-correct static line rather than
            # risk a silent, un-pinged notice nobody notices.
            if outcome == "forbidden" and message.author.mention not in notice:
                return static_fallback
            return notice
        except Exception as e:
            print(f"⚠️ DM notice generation failed for {username} (outcome={outcome}): {e}")
            return static_fallback

    async def classify_server_query(self, message_content: str, username: str) -> str:
        """
        🌸 AI-FIRST server-query router. One cheap Groq call (smallest
        model in MODEL_POOL, max_tokens=6, temperature=0) decides which
        server-info handler this message wants, so paraphrases that don't
        match any hand-written regex still route correctly.

        Returns one of SERVER_QUERY_LABELS. Returns "none" on ANY
        problem — no client, API error, timeout, empty reply, or a label
        that isn't in SERVER_QUERY_LABELS — so the caller's regex chain
        always runs as the fallback. This call is intentionally
        best-effort: it should never be the reason a server-info question
        goes unanswered.
        """
        if not self.client:
            return "none"

        def _call():
            return self.client.chat.completions.create(
                model=CLASSIFIER_MODEL,
                messages=[
                    {"role": "system", "content": SERVER_QUERY_CLASSIFIER_POLICY},
                    {"role": "user", "content": message_content},
                ],
                temperature=0,
                # 🌸 CLASSIFIER_MODEL (openai/gpt-oss-20b) is a REASONING
                # model — it spends tokens on hidden chain-of-thought
                # before emitting any visible content, so max_tokens=6
                # with no reasoning_effort left it burning the entire
                # budget on invisible reasoning before ever reaching the
                # actual label, coming back as empty content (which this
                # function's own except/fallback then silently absorbed
                # as "none" every time — same root cause already found
                # and fixed in groq_pexels.py and groq_exa_search.py).
                # reasoning_effort="low" + enough max_tokens for a short
                # reasoning pass + the label fixes it.
                reasoning_effort="low",
                max_tokens=150,
            )

        try:
            response = await asyncio.to_thread(_call)
            label = (response.choices[0].message.content or "").strip().lower()
            if not label:
                print(f"⚠️ Server-query classifier returned EMPTY content for {username} — check reasoning_effort/max_tokens")
                return "none"
            # 🌸 Defensive parse: strip stray punctuation/quotes the model
            # sometimes wraps a single-word answer in, then take just the
            # first token in case it still adds a trailing word.
            label = re.sub(r"[^a-z_]", " ", label).split()
            label = label[0] if label else ""
            if label not in SERVER_QUERY_LABELS:
                return "none"
            return label
        except Exception as e:
            print(f"⚠️ Server-query classifier error (falling back to regex) for {username}: {e}")
            return "none"

    async def classify_search_intent(self, prompt: str, username: str) -> bool:
        """
        🌸 AI-FIRST search-intent router — same shape as
        classify_server_query above, just a YES/NO instead of a label.
        One cheap Groq call (smallest model in MODEL_POOL, max_tokens=4,
        temperature=0) decides whether `prompt` needs a live web search,
        so paraphrases the SEARCH_INTENT_PATTERN regex would miss (e.g.
        "did they release the sequel yet", "how's Bitcoin doing rn")
        still route to Exa correctly.

        Returns True only on a clean "YES". Returns False on ANY problem
        — no client, API error, timeout, empty reply, or anything that
        isn't recognizably yes/no — so the caller's _wants_web_search
        regex always runs as the fallback. This call is intentionally
        best-effort: it should never be the reason a search request goes
        unanswered, and it should never be the reason an ordinary chat
        reply gets accidentally routed to Exa/compound either.
        """
        if not self.client or not prompt:
            return False

        def _call():
            return self.client.chat.completions.create(
                model=CLASSIFIER_MODEL,
                messages=[
                    {"role": "system", "content": SEARCH_INTENT_CLASSIFIER_POLICY},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                # 🌸 Same reasoning-model fix as classify_server_query
                # above — max_tokens=4 left zero room for gpt-oss-20b's
                # hidden reasoning pass to finish before the budget ran
                # out, so this was almost certainly always returning
                # empty content and silently falling back to False/regex
                # every single call.
                reasoning_effort="low",
                max_tokens=150,
            )

        try:
            response = await asyncio.to_thread(_call)
            verdict = (response.choices[0].message.content or "").strip().upper()
            if not verdict:
                print(f"⚠️ Search-intent classifier returned EMPTY content for {username} — check reasoning_effort/max_tokens")
                return False
            return verdict.startswith("YES")
        except Exception as e:
            print(f"⚠️ Search-intent classifier error (falling back to regex) for {username}: {e}")
            return False

    async def get_ai_response(self, prompt: str, username: str, user_id: int, display_name: str = None, model_id: str = None, react_allowed: bool = False, dm_requested: bool = False, guild=None, channel=None, recent_react_emoji: list[str] = None, shared=None, message_id: int = None, reply_to_message_id: int = None, reply_to_message_text: str = None) -> str | None:
        """
        Runs the (blocking) Groq SDK call in a worker thread so it never
        stalls the bot's event loop. Loads this user's saved chat history
        from groq/memory/{guild_id}/memory.db, or groq/memory/dm/{channel_id}/
        memory.db for DMs (via shared.load_groq_memory) and includes it
        for multi-turn context, then appends this exchange and saves it
        back — trimmed to the last 200 turns so the DB row/prompt don't
        grow unbounded. Memory is now PER-GUILD (and PER-DM-CHANNEL): one
        SQLite file per guild, and a separate one per DM channel, so what
        someone says in Server A no longer bleeds into Server B's context,
        or into their DMs. Within each bucket's file, memory is SHARED
        across every model in MODEL_POOL
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

        🌸 SNOWFLAKE-ANCHORED MEMORY: `message_id` is this user message's
        own Discord snowflake (message.id) and `reply_to_message_id` is
        the snowflake of the bot message THIS message is replying to (if
        any — see bot_service.is_reply_to_bot / message.reference). Every
        turn saved to history now carries its own "message_id", so when
        someone replies to an old bot message instead of just chatting in
        the current thread, the random recent-history window below is
        anchored to END at that old point in time instead of always
        grabbing the newest tail — see the slicing block for details.

        🌸 FALLBACK CONTEXT: `reply_to_message_text` is the ACTUAL text/
        embed content of that replied-to bot message, extracted straight
        from Discord by bot_service._reply_to_bot_context — independent
        of whether it's in Groq's memory at all. Needed because interceptor
        replies (avatar/owner/server-info/media/etc. in handle_mention_reaction)
        never go through get_ai_response, so they never get saved to
        history and can never be found by the anchor search below. When
        that search comes up empty but reply_to_message_text is non-empty,
        that raw text gets injected directly into the prompt instead —
        see the FALLBACK CONTEXT block right after the anchor search.
        """
        if not self.client:
            return None

        if not await self.check_safety(prompt, username):
            return await self._generate_safeguard_decline(username, display_name, guild)

        # 🌸 Random model pick per turn (unless caller pinned model_id) —
        # see MODEL_POOL up top for what's in rotation. EXCEPTION: if the
        # prompt looks like a search request, try Exa FIRST (see
        # ExaSearchService in groq_exa_search.py) instead of forcing
        # groq/compound-mini straight away. Exa does its own
        # search+summarize outside of Groq entirely and hands back a
        # short synthesized string, which sidesteps the whole
        # compound/browser_search 413 saga at the root — that was always
        # caused by raw Tavily search output sharing a TPM budget with
        # the SAME Groq call generating the reply. If Exa is unavailable
        # (no key, exa_py not installed, network blip, empty result) we
        # fall straight back to groq/compound-mini, and the existing
        # slim-retry → browser_search → no-search fallback chain further
        # down still applies exactly as before.
        #
        # 🌸 AI-FIRST, REGEX 2ND: search intent is now decided by
        # classify_search_intent (one cheap Groq call, same shape as
        # classify_server_query) so paraphrases the regex net would miss
        # ("did they release the sequel yet", "how's Bitcoin doing rn")
        # still trigger Exa. _wants_web_search's SEARCH_INTENT_PATTERN
        # regex is kept as the FALLBACK — only consulted when the
        # classifier call itself fails/errors/times out (classify_search_intent
        # already returns False in that case, which would otherwise look
        # identical to a genuine "no search needed" verdict) — never
        # removed, only skipped when the AI call already succeeded.
        # Caller-pinned model_id (e.g. from an explicit /ask-with-model
        # command) still wins over all of this — we only override the
        # RANDOM/search-intent pick, never an explicit one.
        exa_context = None
        if model_id:
            model_to_use = model_id
        else:
            wants_search = await self.classify_search_intent(prompt, username)
            if not wants_search:
                wants_search = _wants_web_search(prompt)

            if wants_search:
                if self.exa:
                    exa_context = await self.exa.search(prompt)
                if exa_context:
                    non_compound_pool = [m for m in MODEL_POOL if "compound" not in m.lower()]
                    model_to_use = random.choice(non_compound_pool) if non_compound_pool else self.default_model
                else:
                    model_to_use = "groq/compound-mini"
            else:
                model_to_use = random.choice(MODEL_POOL)

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
            owner_status=_build_owner_status(user_id),
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

        # 🌸 DM split-routing — only offered in a GUILD (there's a public
        # channel + a private DM to split between there). In a DM, guild
        # is None and "send this to their DMs" is meaningless since
        # they're already IN their DMs. `dm_requested` (computed by the
        # caller from DM_REQUEST_PATTERN — same shape as react_allowed
        # from REACT_REQUEST_PATTERN) strengthens the instruction on
        # turns where the user explicitly asked, instead of leaving
        # detection entirely up to the model's own judgement. See
        # extras/groq_dm_instruct.build_dm_directives for the full logic.
        dm_directives = build_dm_directives(dm_allowed=bool(guild), dm_requested=dm_requested)

        # 🌸 COMPOUND TOKEN BUDGET (part 2): server_context/guild_summary
        # can be genuinely huge on an active server — full role/channel/
        # member dumps easily run several thousand tokens on their own.
        # That's fine for gpt-oss/llama/qwen (large context, no extra
        # tool overhead), but compound/compound-mini ALSO pays token cost
        # for the search tool's query + returned Tavily snippets on top
        # of whatever system prompt we send, and a thin TPM budget can't
        # absorb both. Search questions ("who's winning the world cup",
        # "weather in Jakarta") essentially never need server role/channel
        # data anyway, so just skip building it for compound calls —
        # cheaper AND avoids pulling the model's attention toward
        # server trivia instead of doing the actual search.
        is_compound_call = "compound" in model_to_use.lower()

        # 🌸 Server context — pulled from the v10 REST API and cached (see
        # GroqMentionService.get_server_context_text in groq_service.py) so
        # Groq always knows what server it's replying in, same idea as
        # IDENTITY_INSTRUCTIONS but for "where" instead of "who". Falls
        # back to DM_CONTEXT_INSTRUCTIONS when guild is None, or "" if
        # there's no bot back-reference at all (e.g. running this service
        # standalone outside EnchantedBot).
        if is_compound_call:
            server_context = ""
        elif guild is None:
            # 🌸 DMs have no guild at all — get_server_context_text (and
            # self.guild_info_cache inside it) is guild-shaped (guild.id/
            # guild.name/guild.member_count), so it must never be called
            # with guild=None. This branch has to come BEFORE the
            # self.bot.groq_mentions check below, since that check was
            # true even in DMs (groq_mentions exists regardless of
            # channel type) and was routing DMs into
            # get_server_context_text(None) anyway, crashing on
            # guild.id — see the fallback that was meant to catch this.
            server_context = DM_CONTEXT_INSTRUCTIONS
        elif self.bot and getattr(self.bot, "groq_mentions", None):
            server_context = self.bot.groq_mentions.get_server_context_text(guild)
        else:
            server_context = ""

        # 🌸 Pull compact guild info straight from the per-guild SQLite
        # cache (metadata.db/roles.db/channels.db) instead of dumping the
        # full guild JSON into the prompt. shared.get_guild_context_summary
        # already does the GROUP BY queries + formatting — just await it.
        guild_summary = ""
        if guild and not is_compound_call:
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
        
        personality      = f"{personality}\n\n{react_directives}\n\n{dm_directives}\n\n{identity}\n\n{server_context}\n\n{server_override}\n{guild_summary}".strip()

        # 🌸 EXA SEARCH RESULT — only present when the model-selection
        # block above got a live answer back from Exa for this prompt
        # (see exa_context up top). Injected into the SYSTEM prompt, not
        # the user-facing `prompt` itself, so it never gets saved into
        # Groq memory / bloats future turns' history — it's a one-turn
        # ephemeral grounding, same treatment as guild_summary above.
        if exa_context:
            personality = (
                f"{personality}\n\n"
                "🌸 LIVE SEARCH RESULT (via Exa, fetched just now for this question):\n"
                f"{exa_context}\n\n"
                "Use the above to answer accurately and in your own words/persona — don't just "
                "repeat it verbatim, and don't contradict it with outdated knowledge."
            )

        if reply_to_message_id is not None:
            # 🌸 Only added when the user actually swiped-replied to an old
            # bot message — tells Groq how to read the [REPLYING TO THIS]
            # flags that may appear in recent_history below. Covers BOTH
            # cases: a real anchored exchange found in memory, and the
            # FALLBACK CONTEXT pseudo-turn appended when the replied-to
            # message was an interceptor reply never saved to history
            # (see the slicing block below). Either way, by the time this
            # instruction matters a flagged turn is present in
            # recent_history — if somehow neither fired (e.g. the
            # replied-to message had zero extractable text), this is just
            # harmless unused instruction text.
            anchor_instructions = (
                "⚠️ The user replied directly to one specific earlier message, marked with "
                "[REPLYING TO THIS ⬇️] and [THIS IS THE MESSAGE BEING REPLIED TO] tags below. "
                "Treat THAT exchange as the primary context for their current message — not "
                "whatever else appears nearby in the history."
            )
            personality = f"{personality}\n\n{anchor_instructions}"
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
        # 🌸 DMs (guild=None) now get their own per-channel memory bucket
        # — groq/memory/dm/{channel_id}/memory.db — instead of sharing
        # one flat groq/memory/0/ bucket. channel here is message.channel
        # passed through from handle_mention_reaction; for a DM that's
        # the DMChannel object, and .id is stable per user. Guild
        # messages don't need this — channel_id is ignored whenever
        # guild_id is truthy (see shared._groq_bucket_key).
        dm_channel_id    = channel.id if (channel and not guild) else None
        history          = await shared.load_groq_memory(user_id, guild_id, dm_channel_id)

        # 🌸 SLICING: only ship a random-sized recent window (from THIS
        # user_id's shared history) to Groq to keep input tokens small and
        # vary the model's context a bit turn to turn. `history` itself
        # stays FULL (up to 200 turns) — it's what gets appended to and
        # saved back to memory.db below, so nothing is lost from long-term
        # memory. `recent_history` is just the window actually sent to
        # the API this call: random.randint(2, 8) * 2 gives an even count
        # of 4, 6, 8, 10, 12, 14, or 16 messages.
        #
        # 🌸 ANCHOR POINT: normally the window is just the newest tail of
        # `history` (someone continuing the live conversation). But if
        # `reply_to_message_id` is set — meaning the user swiped up and
        # replied to an OLD bot message instead of the most recent one —
        # we instead anchor the window to END right after that old
        # assistant turn, so the random slice pulls the messages
        # surrounding THAT point in time rather than today's tail. This
        # only works for turns saved after this feature shipped (they
        # carry a "message_id" key); older turns without one are just
        # skipped when searching for the anchor, so nothing crashes on
        # legacy rows — it just falls back to normal tail slicing.
        recent_turn_count = random.randint(2, 8) * 2
        anchor_end = len(history)

        # 🌸 COMPOUND TOKEN BUDGET: groq/compound and groq/compound-mini
        # burn noticeably more tokens per call than a plain chat model —
        # the search tool's query + Tavily's returned snippets all get
        # fed back into the model as EXTRA input tokens on top of
        # whatever history we send, and Groq's free/on-demand TPM ceiling
        # can be as low as ~6-12k tokens/minute. A history window sized
        # fine for gpt-oss/llama can tip a compound call over that limit
        # and 413 ("Request Entity Too Large"). Cap the window hard for
        # compound so there's headroom left for the tool round-trip.
        # On-demand free tier is especially tight, so clamp to just 2 turns
        # (1 exchange) instead of 4 to maximize margin.
        if is_compound_call:
            recent_turn_count = min(recent_turn_count, 2)

        if reply_to_message_id is not None:
            for idx in range(len(history) - 1, -1, -1):
                turn = history[idx]
                if (
                    turn.get("role") == "assistant"
                    and turn.get("message_id") == reply_to_message_id
                ):
                    # 🌸 +1 so the slice INCLUDES this assistant turn
                    # itself (and its matching user turn right before
                    # it), not just everything strictly older than it.
                    anchor_end = idx + 1
                    break
            # 🌸 If no match is found (message too old to still be in the
            # last 200 saved turns, or it predates this feature), anchor_end
            # just stays len(history) — same behavior as before, no crash.

        window_start   = max(0, anchor_end - recent_turn_count)

        # 🌸 HIGHLIGHT FLAG: when there's a real anchor (reply_to_message_id
        # matched something in history), tag the anchored user+assistant
        # pair so Groq can tell "this exact exchange is what they're
        # replying to" apart from the rest of the window, which is just
        # loose surrounding context. Without this, a window that happens
        # to sandwich the anchor next to something more recent/salient
        # (e.g. a GitHub link exchange saved right after it) can pull
        # Groq's attention toward the WRONG turn — it has no way to know
        # which pair in the flat list is the one actually being replied
        # to. anchor_start marks where the flagged pair begins (usually
        # anchor_end - 2, i.e. the user turn immediately followed by the
        # assistant turn that matched reply_to_message_id).
        is_anchored  = reply_to_message_id is not None and anchor_end != len(history)
        anchor_start = anchor_end - 2 if is_anchored else None

        recent_history = []
        for i, t in enumerate(history[window_start:anchor_end], start=window_start):
            content = t["content"]
            if is_anchored and i == anchor_start:
                content = f"[REPLYING TO THIS ⬇️] {content}"
            elif is_anchored and i == anchor_start + 1:
                content = f"[THIS IS THE MESSAGE BEING REPLIED TO] {content}"
            recent_history.append({"role": t["role"], "content": content})

        # 🌸 FALLBACK CONTEXT: fires when this WAS a genuine reply-to-bot
        # (reply_to_message_id is set) but the anchor search above found
        # NOTHING in Groq's memory — meaning the replied-to message was
        # never saved as a history turn at all. This is true for every
        # interceptor reply (avatar/owner/verification/server-info/media/
        # etc. in bot_service.handle_mention_reaction) since those
        # short-circuit and reply BEFORE get_ai_response is ever called.
        # Without this, "summarize it" replying to a server-info embed
        # would fall back to plain tail slicing and Groq would answer
        # about whatever's in today's tail instead (e.g. its own GitHub
        # self-intro) — see the screenshots that prompted this fix.
        # Injected as its own flagged pseudo-turn at the END of
        # recent_history (appended, not spliced into `history` — this
        # never gets saved back to memory.db, it's request-only) so it
        # reads the same way to Groq as a real anchored exchange above.
        if (
            reply_to_message_id is not None
            and not is_anchored
            and reply_to_message_text
        ):
            recent_history.append({
                "role": "assistant",
                "content": f"[THIS IS THE MESSAGE BEING REPLIED TO] {reply_to_message_text}",
            })

        def _call(slim: bool = False):
            # 1. Buat parameter dasar yang selalu digunakan semua model
            # 🌸 slim=True drops recent_history entirely — used for the
            # 413 ("Request Entity Too Large") retry below. Compound
            # calls can tip over a thin TPM budget even with a small
            # history window (search tool round-trip adds its own
            # tokens on top), so on a genuine 413 the safest recovery is
            # just the system prompt + the user's actual message, no
            # conversational context at all.
            messages = [{"role": "system", "content": personality}]
            if not slim:
                messages.extend(recent_history)
            messages.append({"role": "user", "content": prompt})

            # 🌸 Needed one line earlier than before (used for max_tokens
            # below too now) — same value, just computed before kwargs
            # instead of after.
            model_lower = model_to_use.lower()

            # 🌸 COMPOUND TOKEN BUDGET (part 3): even a FULLY slim request
            # (no history, no server context — see used_slim_retry above)
            # can still 413 on compound/compound-mini, because the
            # search-tool round-trip's cost is on TOP of whatever we ask
            # it to output, and Groq's free/on-demand TPM ceiling counts
            # both. This is a known issue on Groq's side — their own
            # community forum has reports of compound 413ing on bare
            # one-line prompts with no system prompt at all — so trimming
            # OUR side further has diminishing returns. What we CAN do is
            # cap the output budget we ask for, since every token we
            # request is TPM we're not leaving for the search round-trip.
            max_tokens_ceiling = 300 if "compound" in model_lower else 1000

            kwargs = {
                "model": model_to_use,
                "messages": messages,
                # 🌸 Randomized per-call (0.10-0.99, 2 decimal places) so
                # replies aren't stuck at one fixed creativity level every
                # turn — same spirit as max_tokens below already varying
                # per call. round() to 2dp keeps the value clean in logs.
                "temperature": round(random.uniform(0.10, 0.99), 2),
                "max_tokens": random.randint(100, max_tokens_ceiling),
            }

            # 2. Atur parameter reasoning secara dinamis sesuai tipe model

            if "compound" in model_lower:
                # 🌸 groq/compound & groq/compound-mini are agentic SYSTEMS,
                # not plain chat models — they reject reasoning_format AND
                # reasoning_effort with a 400. Web search fires automatically
                # server-side (Tavily) whenever the system decides the query
                # needs current info; nothing extra to pass for that. Same
                # temperature/max_tokens kwargs above still apply fine.
                #
                # 🌸 search_settings.include_domains — restricts Tavily to a
                # small trusted allowlist picked from the PROMPT's topic
                # (sports/weather/news/reference — see
                # SEARCH_TOPIC_DOMAINS in groq_instruct.py). Smaller,
                # more relevant search results cost fewer tokens, which
                # is what was tipping compound-mini into 413 ("Request
                # Entity Too Large") on questions like the World Cup one
                # that prompted this. Falls back to None (Groq's normal
                # unrestricted search) when the prompt doesn't match any
                # known topic — an open-ended search question still gets
                # full search rather than a wrong/empty allowlist.
                domains = _search_domains_for_prompt(prompt)
                if domains:
                    kwargs["search_settings"] = {"include_domains": domains}
            elif browser_search_active and "gpt-oss-120b" in model_lower:
                # 🌸 STAGE 2 SEARCH FALLBACK — Groq's browser_search tool is
                # a SEPARATE search mechanism from compound's Tavily
                # integration (different infra, different token-budget
                # profile), so when compound keeps 413ing this gives live
                # search a genuinely different path to succeed on instead
                # of just giving up on real-time info. tool_choice="auto"
                # lets the model decide whether the prompt actually needs
                # a search instead of forcing one on every call to this
                # fallback — "required" was firing browser_search even on
                # prompts that didn't need it, burning tokens/latency for
                # nothing. gpt-oss-120b with tool use wants
                # max_completion_tokens instead of max_tokens, and top_p=1
                # — matches Groq's documented working example for this tool.
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = random.randint(200, 500)
                kwargs["top_p"] = 1
                # 🌸 Lower/narrower temperature than the usual randomized
                # 0.10-0.99 range — this call is meant to ground a factual
                # summary in real search results, not go for creative
                # variety, so keep it closer to deterministic.
                kwargs["temperature"] = round(random.uniform(0.10, 0.40), 2)
                kwargs["tools"] = [{"type": "browser_search"}]
                kwargs["tool_choice"] = "auto"
                kwargs["reasoning_format"] = "hidden"
                kwargs["reasoning_effort"] = "low"
            elif "qwen" in model_lower:
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

            # 3. Jalankan request dengan mendekompresi (unpack) kwargs
            return self.client.chat.completions.with_raw_response.create(**kwargs)

        used_slim_retry = False
        # 🌸 STAGE 2 flags — a 413 that survives the slim retry falls
        # through to gpt-oss-120b + Groq's browser_search tool (a
        # DIFFERENT search mechanism from compound's Tavily integration —
        # see _call above). used_browser_search_fallback is the monotonic
        # "already tried stage 2" marker (prevents retrying it twice);
        # browser_search_active is only True WHILE that attempt is live,
        # so _call knows to attach the tool — it gets flipped back off if
        # we fall through to stage 3.
        used_browser_search_fallback = False
        browser_search_active = False
        # 🌸 STAGE 3 — browser_search fallback 413'd too, so live search
        # genuinely isn't happening this turn on either mechanism. Give up
        # and answer from the model's own knowledge instead of returning
        # None. See the GroqAPIStatusError handler below.
        used_final_fallback = False
        # 🌸 +3 (not +1) so the 413 slim-retry, the browser_search
        # fallback, AND the final no-search fallback ALL get their own
        # iteration even with a single API key — without the extra slots,
        # a lone key runs the loop out of iterations partway through a
        # `continue` chain and a later-stage retry never actually happens.
        max_attempts = max(len(self.api_keys), 1) + 3
        for attempt in range(max_attempts):
            try:
                raw_response = await asyncio.to_thread(_call, used_slim_retry)
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
                    reply = await self._generate_safeguard_decline(username, display_name, guild)
                    self._log_ai_transcript(
                        username, user_id, model_to_use, guild, channel,
                        prompt, "[BLOCKED BY OUTPUT SAFEGUARD — reply withheld]",
                    )
                else:
                    # 🌸 Full prompt+response transcript → log/groq_ai.log
                    self._log_ai_transcript(username, user_id, model_to_use, guild, channel, prompt, reply)

                # 🌸 message_id on the user turn = this incoming message's
                # own snowflake. The assistant turn doesn't have its sent
                # message's snowflake yet at this point (message.reply()
                # hasn't happened — that's back in bot_service.py), so it's
                # tagged with the SAME message_id as the user turn it's
                # replying to. That's enough for the anchor search above:
                # replying to a bot message later just needs to match
                # message_id somewhere on an "assistant" row, and since the
                # user+assistant pair share one id, a reply targeting
                # either the user's message or the bot's reply resolves to
                # the same anchor point.
                history.append({"role": "user", "content": prompt, "message_id": message_id})
                history.append({"role": "assistant", "content": reply, "message_id": message_id})
                await shared.save_groq_memory(model_to_use, username, user_id, history[-200:], guild_id, dm_channel_id)

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
            except GroqAPIStatusError as e:
                # 🌸 413 "Request Entity Too Large" — most common on
                # compound/compound-mini, whose search-tool round-trip
                # (query + Tavily snippets fed back in) adds tokens on
                # top of whatever history we sent, tipping a thin TPM
                # budget over the edge. ONE retry with recent_history
                # dropped entirely (slim=True — just system prompt + the
                # user's actual message) before falling back further. Any
                # other non-429 status code (400, 500, etc.) falls
                # through to the generic handler below unchanged — this
                # branch only ever retries on a genuine 413.
                if getattr(e, "status_code", None) == 413 and not used_slim_retry:
                    err_headers = getattr(getattr(e, "response", None), "headers", None)
                    if err_headers:
                        self._log_rate_limits(err_headers, username, model_to_use)
                    print(f"⚠️ Groq 413 on {model_to_use} — retrying once with history stripped...")
                    used_slim_retry = True
                    continue

                # 🌸 STAGE 2 — a 413 that survives the slim retry means
                # trimming OUR side of the request didn't help, which
                # tracks: Groq's own community forum has reports of
                # compound/compound-mini 413ing on bare one-line prompts
                # with NO system prompt at all, because the search-tool
                # round-trip's token cost is added server-side and isn't
                # something we control from the request we send. Instead
                # of giving up on live search immediately, try Groq's
                # OTHER built-in search tool — browser_search on
                # gpt-oss-120b — since it's separate infra from compound's
                # Tavily integration and may not be hitting the same
                # budget wall.
                if (
                    getattr(e, "status_code", None) == 413
                    and used_slim_retry
                    and is_compound_call
                    and not used_browser_search_fallback
                ):
                    err_headers = getattr(getattr(e, "response", None), "headers", None)
                    if err_headers:
                        self._log_rate_limits(err_headers, username, model_to_use)
                    print(f"⚠️ Groq 413 persisted on {model_to_use} — trying gpt-oss-120b + browser_search instead...")
                    used_browser_search_fallback = True
                    browser_search_active = True
                    model_to_use = "openai/gpt-oss-120b"
                    is_compound_call = False
                    continue

                # 🌸 STAGE 3 — the browser_search fallback ALSO 413'd, so
                # live search genuinely isn't available on either
                # mechanism this turn. Drop search entirely and retry ONCE
                # more on a plain model from the pool — the user still
                # gets an actual reply, just without real-time info, and
                # we say so up front so the model doesn't confidently make
                # something up in its place instead of just returning
                # None (dead silence + a scary error dump to the log
                # channel, which is what was happening before).
                if (
                    getattr(e, "status_code", None) == 413
                    and used_browser_search_fallback
                    and not used_final_fallback
                ):
                    err_headers = getattr(getattr(e, "response", None), "headers", None)
                    if err_headers:
                        self._log_rate_limits(err_headers, username, model_to_use)
                    print(f"⚠️ Groq 413 persisted on browser_search too — dropping search entirely, retrying plain...")
                    used_final_fallback = True
                    browser_search_active = False
                    fallback_pool = [
                        m for m in MODEL_POOL
                        if "compound" not in m.lower() and "gpt-oss-120b" not in m.lower()
                    ]
                    model_to_use = random.choice(fallback_pool) if fallback_pool else self.default_model
                    prompt = (
                        f"{prompt}\n\n(Note: live web search wasn't available just now — "
                        "answer from what you already know, and briefly mention you couldn't "
                        "pull live results instead of guessing at current facts.)"
                    )
                    continue

                err_headers = getattr(getattr(e, "response", None), "headers", None)
                if err_headers:
                    self._log_rate_limits(err_headers, username, model_to_use)
                print(f"⚠️ Groq APIStatusError (status_code={getattr(e, 'status_code', '?')}): {e}")
                return None
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
