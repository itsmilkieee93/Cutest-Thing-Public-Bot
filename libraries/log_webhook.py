"""
🌸 Cutest Thing — Live Log-to-Discord Webhook
Wires directly into bot_service.py: hooks sys.stdout/sys.stderr so every
print()/traceback the bot emits gets classified (success/warning/error)
and shipped as a color-coded embed to a private channel via webhook —
no separate process, no bot.log file needed.

Usage (in bot_service.py, near the top, before anything else prints):

    from log_webhook import start_log_webhook
    start_log_webhook()

That's it — stdout/stderr are now tee'd to Discord for the rest of the
process lifetime. Terminal output still works exactly as before.
"""

import sys
import re
import time
import asyncio
import threading
from collections import deque
from datetime import datetime, timezone

import aiohttp

import config_loader

# ── Tunables ──────────────────────────────────────────────────────────
BATCH_WINDOW = 4.0           # seconds to accumulate lines before flushing one embed
MAX_LINES_PER_BATCH = 300    # safety cap so crash-spam can't flood the channel (was 15 — that's
                              # what was silently dropping most of a batch and forcing hard
                              # truncation of tracebacks)
MAX_FIELD_LEN = 1970         # a group's text can live in one embed's description up to this
                              # length — kept under 2000 once wrapped in ``` fences (6 chars).
                              # Longer groups are split across multiple embeds of this size
                              # instead of being truncated or sent as a file attachment.
MAX_TOTAL_EMBED_CHARS = 5500 # stay under Discord's hard 6000-char total-embed limit (with headroom)
MAX_EMBEDS_PER_MESSAGE = 10  # Discord's hard limit on embeds per webhook message
FLUSH_QUEUE_MAX = 2000       # drop oldest if this backs up (webhook down / no internet)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 🌸 Severity detection — order matters, first match wins.
# Tuned to the bot's own emoji/print conventions (⚠️ warnings, ❌/Exception
# errors) plus common Python traceback / logging patterns.
SEVERITY_PATTERNS = [
    ("error", re.compile(r"❌|Traceback \(most recent call last\)|CRITICAL|"
                          r"\bERROR\b|Exception:|Error:|Startup Error", re.IGNORECASE)),
    ("warning", re.compile(r"⚠️|\bWARNING\b|\bWARN\b|Forbidden|Rate limited", re.IGNORECASE)),
    ("success", re.compile(r"✅|🌸 Logged in as|Synced \d+ commands|Commands Synced")),
]

SEVERITY_STYLE = {
    "success": {"color": 0x57F287, "emoji": "✅", "label": "Success"},   # Discord green
    "warning": {"color": 0xFEE75C, "emoji": "⚠️", "label": "Warning"},   # Discord yellow
    "error":   {"color": 0xED4245, "emoji": "❌", "label": "Error"},     # Discord red
    "info":    {"color": 0x5865F2, "emoji": "ℹ️", "label": "Info"},      # Discord blurple, default bucket
}


def classify(line: str) -> str:
    """Return 'success' | 'warning' | 'error' | 'info' for a single log line."""
    for severity, pattern in SEVERITY_PATTERNS:
        if pattern.search(line):
            return severity
    return "info"


def chunk_lines(group_lines: list[str], max_len: int) -> list[str]:
    """🌸 Split a list of log lines into text chunks that each stay under
    max_len chars, breaking on line boundaries so a traceback frame is
    never sliced mid-line. If a single line is itself longer than
    max_len, it gets hard-wrapped as a last resort."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in group_lines:
        line_len = len(line) + 1  # +1 for the joining newline
        if current and current_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if line_len > max_len:
            # 🌸 One monster line on its own — hard-wrap it so it still
            # fits rather than blowing the budget of every chunk it touches.
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i + max_len])
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


class _Tee:
    """File-like object that writes to the real stream AND queues a copy
    for shipping to Discord. Swapped in for sys.stdout / sys.stderr."""

    def __init__(self, real_stream, shipper: "LogShipper"):
        self._real = real_stream
        self._shipper = shipper
        self._line_buf = ""

    def write(self, data: str):
        self._real.write(data)  # terminal/bot.log behavior is unchanged
        self._line_buf += data
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            clean = ANSI_RE.sub("", line).rstrip()
            if clean:
                self._shipper.enqueue(clean)
        return len(data)

    def flush(self):
        self._real.flush()

    def isatty(self):
        return getattr(self._real, "isatty", lambda: False)()

    def fileno(self):
        # 🌸 Some libraries (e.g. speedtest-cli) grab sys.stdout's raw fd
        # directly — forward to the real stream so they still work.
        return self._real.fileno()

    def writable(self):
        return getattr(self._real, "writable", lambda: True)()

    def readable(self):
        return getattr(self._real, "readable", lambda: False)()

    def seekable(self):
        return getattr(self._real, "seekable", lambda: False)()

    def __getattr__(self, name):
        # 🌸 Catch-all passthrough: anything we haven't explicitly wrapped
        # (encoding, buffer, mode, closed, etc.) goes straight to the real
        # stream so _Tee behaves like a transparent wrapper, not a stub.
        return getattr(self._real, name)


class _LiveStdoutStream:
    """File-like target for logging.StreamHandler that always forwards to
    the CURRENT sys.stdout, read fresh on every write — never a reference
    captured at import time. Needed because a module's own FileHandler
    logger might get set up before start_log_webhook() has installed the
    _Tee (depends on import order in bot_service.py); grabbing sys.stdout
    fresh sidesteps that ordering problem entirely."""

    def write(self, msg):
        sys.stdout.write(msg)

    def flush(self):
        sys.stdout.flush()


def add_stdout_relay(logger, prefix: str = None, level=None):
    """🌸 Attach a relay handler to any logging.Logger so its messages ALSO
    flow to stdout — and therefore to Discord, once start_log_webhook() is
    active — in addition to whatever FileHandler(s) it already has.

    Usage, in any module with its own file-only logger:

        from log_webhook import add_stdout_relay
        add_stdout_relay(my_logger, prefix="MyModule")

    `prefix` tags each line (e.g. "[MyModule]") so it's identifiable once
    mixed in with everything else streaming to the status channel. Safe
    to call more than once on the same logger (e.g. across hot-reloads) —
    won't stack duplicate relay handlers.
    """
    marker = "_is_live_stdout_relay"
    if any(getattr(h, marker, False) for h in logger.handlers):
        return  # already attached — don't stack a duplicate

    import logging as _logging
    tag = prefix if prefix is not None else logger.name
    handler = _logging.StreamHandler(_LiveStdoutStream())
    handler.setLevel(level if level is not None else _logging.INFO)
    handler.setFormatter(_logging.Formatter(f"[{tag}] [%(levelname)s] %(message)s"))
    setattr(handler, marker, True)
    logger.addHandler(handler)


class LogShipper:
    """Owns a background asyncio loop (its own thread) that batches queued
    lines by severity and POSTs them to the Discord webhook as embeds."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self):
        self._thread.start()

    def enqueue(self, line: str):
        with self._lock:
            self._queue.append(line)
            if len(self._queue) > FLUSH_QUEUE_MAX:
                self._queue.popleft()  # drop oldest, keep most recent context

    def _drain(self) -> list[str]:
        with self._lock:
            lines = list(self._queue)
            self._queue.clear()
        return lines

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        async with aiohttp.ClientSession() as session:
            self._session = session
            while True:
                await asyncio.sleep(BATCH_WINDOW)
                lines = self._drain()
                if not lines:
                    continue
                await self._ship_batch(session, lines)

    async def _ship_batch(self, session: aiohttp.ClientSession, lines: list[str]):
        """Group consecutive same-severity lines together so a batch with
        one warning + a wall of info doesn't get flattened into one color.

        Long groups (full tracebacks etc.) are split across multiple
        embeds — each kept under 2000 chars — instead of being truncated
        or sent as a file attachment. Everything ships as embeds only."""
        groups: list[tuple[str, list[str]]] = []
        for line in lines[:MAX_LINES_PER_BATCH]:
            severity = classify(line)
            if groups and groups[-1][0] == severity:
                groups[-1][1].append(line)
            else:
                groups.append((severity, [line]))

        dropped = len(lines) - MAX_LINES_PER_BATCH
        all_embeds = []
        running_total = 0

        for severity, group_lines in groups:
            style = SEVERITY_STYLE[severity]
            chunks = chunk_lines(group_lines, MAX_FIELD_LEN)
            total_parts = len(chunks)
            for part_i, chunk_text in enumerate(chunks, start=1):
                title = f"{style['emoji']} {style['label']}"
                if total_parts > 1:
                    title += f" ({part_i}/{total_parts})"
                desc = f"```{chunk_text}```"
                cost = len(title) + len(desc)
                all_embeds.append({
                    "title": title,
                    "description": desc,
                    "color": style["color"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "_cost": cost,
                })

        note = []
        if dropped > 0:
            note.append(f"{dropped} line(s) over the per-batch cap")

        # 🌸 Send in message-sized batches: up to MAX_EMBEDS_PER_MESSAGE
        # embeds and MAX_TOTAL_EMBED_CHARS total per webhook POST. Nothing
        # gets omitted — a batch that's too big for one message just
        # becomes multiple messages.
        message_embeds: list[dict] = []
        message_cost = 0
        for embed in all_embeds:
            cost = embed.pop("_cost")
            fits_count = len(message_embeds) < MAX_EMBEDS_PER_MESSAGE
            fits_budget = message_cost + cost <= MAX_TOTAL_EMBED_CHARS
            if message_embeds and not (fits_count and fits_budget):
                await self._post(session, message_embeds)
                message_embeds = []
                message_cost = 0
            message_embeds.append(embed)
            message_cost += cost

        if note:
            note_embed = {
                "title": "⚠️ Warning",
                "description": f"```{' | '.join(note)} — spamming too fast```",
                "color": SEVERITY_STYLE["warning"]["color"],
            }
            note_cost = len(note_embed["title"]) + len(note_embed["description"])
            if message_embeds and (
                len(message_embeds) >= MAX_EMBEDS_PER_MESSAGE
                or message_cost + note_cost > MAX_TOTAL_EMBED_CHARS
            ):
                await self._post(session, message_embeds)
                message_embeds = []
            message_embeds.append(note_embed)

        if message_embeds:
            await self._post(session, message_embeds)

    async def _post(self, session: aiohttp.ClientSession, embeds: list[dict]):
        backoff = [0, 1, 2, 5, 10]
        for delay in backoff:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with session.post(self.webhook_url, json={"embeds": embeds}) as resp:
                    if resp.status in (200, 204):
                        return
                    if resp.status == 429:
                        try:
                            retry_after = float((await resp.json()).get("retry_after", 2))
                        except Exception:
                            retry_after = 2
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 400:
                        # 🌸 400 means the payload itself is bad (too long,
                        # malformed, etc.) — retrying won't help, so surface
                        # the reason once and move on instead of burning
                        # the whole backoff ladder.
                        try:
                            body = await resp.text()
                        except Exception:
                            body = "(no body)"
                        self._real_print(f"⚠️ log_webhook: Discord returned 400 — {body[:300]}")
                        return
                    self._real_print(f"⚠️ log_webhook: Discord returned {resp.status}")
            except aiohttp.ClientError as e:
                self._real_print(f"⚠️ log_webhook: send failed — {e}")
        self._real_print("❌ log_webhook: giving up on this batch after retries")

    @staticmethod
    def _real_print(msg: str):
        # Bypasses the Tee to avoid ever re-queueing our own error messages
        sys.__stdout__.write(msg + "\n")


_shipper_instance = None  # module-level guard so start_log_webhook() is idempotent


def start_log_webhook():
    """🌸 Call once, as early as possible in bot_service.py. Hooks stdout
    and stderr so everything the bot prints (including tracebacks) also
    streams to the private log channel as color-coded embeds."""
    global _shipper_instance
    if _shipper_instance is not None:
        return  # already started — safe to call multiple times (e.g. on reload)

    webhook_url = config_loader.get_log_webhook_url()
    if not webhook_url or "XXXXXXXXXX" in webhook_url:
        print("⚠️ log_webhook: no log_webhook_url configured in discord_config.py "
              "→ WEBHOOKS — Discord log streaming disabled.")
        return

    shipper = LogShipper(webhook_url)
    shipper.start()

    sys.stdout = _Tee(sys.stdout, shipper)
    sys.stderr = _Tee(sys.stderr, shipper)

    _shipper_instance = shipper
    print("🌸 log_webhook: stdout/stderr now streaming to Discord")
