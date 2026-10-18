"""🌸 Logging/embeds module — extracted from groq_ai.py's monolithic
GroqService. Everything here is moved VERBATIM (same logic, same format
strings, same file paths) — this is a structure-only split, not a
rewrite. See groq_ai.py's module docstring / the refactor task notes
for the full behavioral contract this preserves.

Covers:
  - The three module-level loggers (groq_logger, groq_ai_logger,
    _groq_ai_relay_logger) and their file/relay setup.
  - _extract_ratelimit_headers / _parse_groq_429 (pure functions, no
    self needed — kept as free functions here, called from
    GroqService via thin instance-method wrappers so external callers
    that do `self._extract_ratelimit_headers(...)` etc. keep working).
  - _build_rate_limit_embed / _build_success_embed (need `self` for
    self.current_key_index / thumbnail constants — kept as functions
    that take the service instance explicitly, wired onto GroqService
    as bound methods from service.py).
  - _log_ai_transcript / _log_rate_limits.

🌸 SUCCESS_THUMBNAIL_URL / FAIL_THUMBNAIL_URL stay defined here since
the embed builders are the only things that use them, but they're
also re-exported so service.py can still expose them as class
attributes on GroqService (some external code may reference
GroqService.SUCCESS_THUMBNAIL_URL directly).
"""
import os
import re
import time
import logging
import discord
from collections import OrderedDict
from datetime import datetime

from log_webhook import add_stdout_relay

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


# 🌸 Square thumbnail shown in the corner of the log embeds.
SUCCESS_THUMBNAIL_URL = "https://c.tenor.com/TcMXxO_U0dgAAAAC/tenor.gif"
FAIL_THUMBNAIL_URL    = "https://c.tenor.com/Sn0nQ5dvHm4AAAAC/tenor.gif"


def _extract_ratelimit_headers(headers) -> dict:
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


def _parse_groq_429(error: Exception) -> dict:
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


def _build_rate_limit_embed(service, error: Exception, username: str, user_id: int, model_id: str) -> discord.Embed:
    """🌸 Rich embed for a Groq 429 — deliberately omits the
    console.groq.com billing/upgrade link Groq includes in the raw
    error text. `service` is the owning GroqService instance (needed
    for current_key_index)."""
    parsed = _parse_groq_429(error)

    embed = discord.Embed(
        title="🚫 Groq Rate Limit Hit (429)",
        color=0xE74C3C,
        timestamp=datetime.now(),
    )
    embed.add_field(name="🧠 Model", value=f"`{model_id}`", inline=True)
    embed.add_field(name="🔑 Key", value=f"#{service.current_key_index + 1}", inline=True)
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
    embed.set_thumbnail(url=FAIL_THUMBNAIL_URL)
    embed.set_footer(text="Cutest Thing 🌸 | Groq rate-limit alert")
    return embed


# ─────────────────────────────────────────────────────────────────────
# 🌸 USER-FACING AI ERROR EMBEDS
#
# _build_rate_limit_embed above is the ADMIN embed (org id, key index,
# usage numbers) that goes to the log channels. The functions below build
# the opposite: a tiny embed for the person who talked to the bot when the
# AI provider answers with anything other than 200. It never contains the
# raw provider text, org id, key number or billing link.
# ─────────────────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|d|h|m|s)", re.IGNORECASE)
_DURATION_UNITS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def _parse_duration_seconds(text) -> float | None:
    """"7m12.768s" / "2h3m" / "850ms" / "30" (bare seconds) -> seconds, else None."""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    parts = _DURATION_RE.findall(text)
    if parts:
        return sum(float(value) * _DURATION_UNITS[unit.lower()] for value, unit in parts)
    try:
        return float(text)
    except ValueError:
        return None


def get_error_status(error: Exception) -> int | None:
    """HTTP status code of a provider error (groq.APIStatusError exposes
    .status_code; .response.status_code is the fallback), or None when the
    exception has no HTTP status (connection error, timeout, plain bug)."""
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _retry_after_seconds(error: Exception, limit_type: str) -> float | None:
    """How long until the limit resets. Order of trust: the Retry-After
    header, the "try again in 7m12s" text in the error body, then the
    x-ratelimit-reset-* header matching the limit that was hit."""
    headers = getattr(getattr(error, "response", None), "headers", None)
    candidates = []
    if headers:
        try:
            candidates.append(headers.get("retry-after"))
        except Exception:
            pass
    candidates.append(_parse_groq_429(error).get("retry_after"))
    if headers:
        try:
            kind = "tokens" if "token" in limit_type.lower() else "requests"
            candidates.append(headers.get(f"x-ratelimit-reset-{kind}"))
        except Exception:
            pass
    for raw in candidates:
        seconds = _parse_duration_seconds(raw)
        if seconds is not None and seconds >= 0:
            return seconds
    return None


def build_user_error_embed(error: Exception) -> discord.Embed | None:
    """Minimal embed for the user who triggered a failed AI call, or None
    when `error` isn't a provider/connection error (so the caller can
    re-raise real bugs untouched). Rate limits (429) say whether the
    token or the request quota ran out and show when it resets as a live
    Discord timestamp (<t:...:R> renders as "in 7 minutes")."""
    status = get_error_status(error)
    if status is None:
        if type(error).__name__ not in ("APIConnectionError", "APITimeoutError"):
            return None
        title, desc, color = (
            "📡 AI is unreachable",
            "I couldn't reach the AI service. Please try again in a moment.",
            0x95A5A6,
        )
        footer = "Connection error"
    elif status == 429:
        limit_type = _parse_groq_429(error).get("limit_type") or ""
        lowered = limit_type.lower()
        what = "Token" if "token" in lowered else "Request" if "request" in lowered else "Usage"
        period = "daily " if "per day" in lowered else "per-minute " if "per minute" in lowered else ""
        seconds = _retry_after_seconds(error, limit_type)
        if seconds is not None:
            reset = f"Resets <t:{int(time.time() + seconds) + 1}:R>."
        else:
            reset = "Please try again in a little while."
        title = f"⏳ {what} limit reached"
        desc = f"My {period}{what.lower()} quota is used up. {reset}"
        color = 0xF39C12
        footer = "Error 429"
    elif status == 401:
        title, desc, color = (
            "🔑 AI key rejected",
            "The AI service rejected my credentials. Please let the bot owner know.",
            0xE74C3C,
        )
        footer = "Error 401"
    elif status == 403:
        title, desc, color = (
            "🚫 AI access denied",
            "The AI service refused this request. Please let the bot owner know.",
            0xE74C3C,
        )
        footer = "Error 403"
    elif status == 404:
        title, desc, color = (
            "❓ AI model not found",
            "The AI model I'm set up to use wasn't found. Please let the bot owner know.",
            0xE74C3C,
        )
        footer = "Error 404"
    elif status == 413:
        title, desc, color = (
            "📏 Message too large",
            "That message (with its context) is too big for the AI. Try something shorter.",
            0xF39C12,
        )
        footer = "Error 413"
    elif status >= 500:
        title, desc, color = (
            "🛠️ AI service is having issues",
            "The AI provider had a problem on its side. Please try again soon.",
            0x95A5A6,
        )
        footer = f"Error {status}"
    else:
        title, desc, color = (
            "⚠️ AI request failed",
            "The AI service couldn't process that request. Please try again.",
            0xE74C3C,
        )
        footer = f"Error {status}"

    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text=footer)
    return embed


def _build_success_embed(service, username: str, user_id: int, guild, channel, model_id: str, headers=None) -> discord.Embed:
    """🌸 Green success-log embed sent after every completed Groq reply,
    with user/server/channel snowflake IDs for tracing, plus every raw
    x-ratelimit-* header Groq sent back for that request. `service` is
    the owning GroqService instance (kept for parity with
    _build_rate_limit_embed's signature; not otherwise used)."""
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
    rl_headers = _extract_ratelimit_headers(headers)
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

    embed.set_thumbnail(url=SUCCESS_THUMBNAIL_URL)
    embed.set_footer(text="Cutest Thing 🌸 | Groq activity log")
    return embed


def _log_ai_transcript(username: str, user_id: int, model_id: str,
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


def _log_rate_limits(service, headers, username: str, model_id: str):
    """🌸 Pulls every x-ratelimit-* header off a Groq response (requests/
    tokens limit, remaining, reset — whatever Groq is currently sending)
    and appends a line to logs/bot.log via groq_logger. Best-effort: any
    failure here just prints a warning and never breaks the actual reply.
    `service` is the owning GroqService instance (needed for
    current_key_index)."""
    try:
        limit_headers = {
            k: v for k, v in headers.items()
            if k.lower().startswith("x-ratelimit")
        }
        if limit_headers:
            summary = ", ".join(f"{k}={v}" for k, v in sorted(limit_headers.items()))
            groq_logger.info(f"[{username} | {model_id} | Key #{service.current_key_index + 1}] {summary}")
    except Exception as e:
        print(f"⚠️ Rate-limit log error: {e}")


# 🌸 Side channel for the user-facing error embed. run_completion_loop
# swallows provider errors and returns None (callers rely on that), so it
# records the LAST error here, keyed by the incoming Discord message id, and
# groq_service.py pops it when it gets None back. Bounded so a record whose
# message never gets popped can't pile up.
_LAST_AI_ERRORS: "OrderedDict[int, Exception]" = OrderedDict()
_LAST_AI_ERRORS_MAX = 200


def remember_ai_error(message_id, error: Exception) -> None:
    """Called from run_completion_loop right before it gives up (returns None)."""
    if message_id is None:
        return
    _LAST_AI_ERRORS[message_id] = error
    _LAST_AI_ERRORS.move_to_end(message_id)
    while len(_LAST_AI_ERRORS) > _LAST_AI_ERRORS_MAX:
        _LAST_AI_ERRORS.popitem(last=False)


def pop_ai_error(message_id) -> Exception | None:
    """Removes and returns the recorded error for this message (or None)."""
    if message_id is None:
        return None
    return _LAST_AI_ERRORS.pop(message_id, None)
