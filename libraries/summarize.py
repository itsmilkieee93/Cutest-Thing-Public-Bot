"""
📝 summarize.py
Discord cog for the /summarize slash command
Mirrors the Cloudflare AI Gateway infrastructure from cloudflare_ai.py
(Stateless / No Memory)

Auth files, neuron tracking, and token rotation follow the same pattern
as cloudflare_ai.py — shared auth files are read fresh before each call
so parallel cog interactions don't clobber each other's usage counters.

Summary is delivered as a styled Discord embed.
"""

import re
import json
import random
import discord
import aiohttp
import asyncio
import os
from datetime import timezone
from discord import app_commands
from discord.ext import commands
from resources.shared import bridge_log

# 🌸 Dynamic personality — instructions are generated live from the bot's
# CURRENT per-guild nickname (set via /server-persona-set), same system
# groq_ai.py uses, instead of a static personality.txt file.
from personality import load_personality

# ─────────────────────────────────────────────────────────────────────────────
# Pastel palette & loading GIFs  (from chatting_fun.py)
# ─────────────────────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────
# Paths & constants
# ─────────────────────────────────────────────────────────────────────────────
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_AUTH_DIR   = os.path.join(_BASE_DIR, "..", "auth")

import sys
if _AUTH_DIR not in sys.path:
    sys.path.insert(0, _AUTH_DIR)
import key_config  # lives at auth/key_config.py, gitignored — see generate_key_config.py

CLOUDFLARE_AUTH_SETS = [
    key_config.CLOUDFLARE_AI,
    key_config.CLOUDFLARE_AI_2,
    key_config.CLOUDFLARE_AI_3,
]
AUTH_FILES = CLOUDFLARE_AUTH_SETS  # alias kept so len(AUTH_FILES) usages below still work

USAGE_PATH        = os.path.join(_AUTH_DIR, "cloudflare_usage.json")

# Discord v10 REST base
DISCORD_API_BASE  = "https://discord.com/api/v10"

NEURON_SWITCH_THRESHOLD = 10_000
_ROTATE_ON_STATUS       = {401, 403, 429, 404}

# ─────────────────────────────────────────────────────────────────────────────
# Model catalogue  (mirrors cloudflare_ai.py)
# ─────────────────────────────────────────────────────────────────────────────
_MODEL_NEURON_FACTOR: dict[str, float] = {
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast":  0.033,
    "@cf/meta/llama-3.2-11b-vision-instruct":   0.048,
    "@cf/meta/llama-3.1-70b-instruct-fp8-fast": 0.080,
    "@cf/qwen/qwen3-30b-a3b-fp8":               0.072,
    "@cf/google/gemma-4-26b-a4b-it":            0.085,
}

DEFAULT_MODEL = "@cf/meta/llama-3.1-8b-instruct-fp8-fast"

MODEL_CHOICES = [
    app_commands.Choice(
        name="⚡ Llama 3.1 8B Fast  [10–35 neurons]",
        value="@cf/meta/llama-3.1-8b-instruct-fp8-fast",
    ),
    app_commands.Choice(
        name="👁️ Llama 3.2 11B Vision  [20–55 neurons]",
        value="@cf/meta/llama-3.2-11b-vision-instruct",
    ),
    app_commands.Choice(
        name="🦙 Llama 3.1 70B Titan  [45–95 neurons]",
        value="@cf/meta/llama-3.1-70b-instruct-fp8-fast",
    ),
    app_commands.Choice(
        name="🎨 Qwen3 30B Creative  [40–85 neurons]",
        value="@cf/qwen/qwen3-30b-a3b-fp8",
    ),
    app_commands.Choice(
        name="🌟 Gemma 4 26B Google  [50–95 neurons]",
        value="@cf/google/gemma-4-26b-a4b-it",
    ),
]

# Human-readable label for each model value (used in embed footer)
_MODEL_LABELS: dict[str, str] = {
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast":  "⚡ Llama 3.1 8B Fast",
    "@cf/meta/llama-3.2-11b-vision-instruct":   "👁️ Llama 3.2 11B Vision",
    "@cf/meta/llama-3.1-70b-instruct-fp8-fast": "🦙 Llama 3.1 70B Titan",
    "@cf/qwen/qwen3-30b-a3b-fp8":               "🎨 Qwen3 30B Creative",
    "@cf/google/gemma-4-26b-a4b-it":            "🌟 Gemma 4 26B Google",
}

# ─────────────────────────────────────────────────────────────────────────────
# Leaked chat-template tag patterns some models append to replies
# ─────────────────────────────────────────────────────────────────────────────
_JUNK_PATTERNS = [
    r"</assistant>\s*$",
    r"<\|im_end\|>\s*$",
    r"\[/INST\]\s*$",
    r"<</?SYS>>\s*$",
    r"<\|end_of_turn\|>\s*$",
    r"<\|eot_id\|>\s*$",
]


def _clean_reply(text: str) -> str:
    """Strip leaked model chat-template artifacts from the end of replies."""
    for pattern in _JUNK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────
def _load_auth(auth_set: dict) -> tuple[str, str, list]:
    """
    Load a cloudflare_ai auth set from key_config.py.
    auth_set is a dict: {"account_id": ..., "gateway_id": ..., "tokens": [...]}
    Returns (account_id, gateway_id, [token, ...]).
    """
    account_id = (auth_set or {}).get("account_id", "").strip()
    gateway_id = (auth_set or {}).get("gateway_id", "").strip()
    tokens     = [t.strip() for t in (auth_set or {}).get("tokens", []) if t.strip()]

    if not account_id or not gateway_id or not tokens:
        print(f"⚠️ Summarize: incomplete auth set in key_config.py "
              f"(need account_id, gateway_id, and at least one token)")
        return "", "", []

    return account_id, gateway_id, tokens


def _load_discord_token() -> str:
    """Read the bot token from key_config.DISCORD_TOKEN."""
    token = (key_config.DISCORD_TOKEN or "").strip()
    if not token:
        print("⚠️ Summarize: key_config.DISCORD_TOKEN is empty or unset!")
    return token


# ─────────────────────────────────────────────────────────────────────────────
# Neuron usage persistence
# ─────────────────────────────────────────────────────────────────────────────
def _load_usage() -> dict:
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "current_file_idx": 0,
            "neurons": {str(i): 0.0 for i in range(len(AUTH_FILES))},
        }


def _save_usage(usage: dict):
    try:
        os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
        with open(USAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(usage, f, indent=2)
    except Exception as e:
        print(f"⚠️ Summarize: Could not save usage: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────
class SummarizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh_state()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _refresh_state(self):
        """Reload shared auth + usage state from disk before each request."""
        self._usage    = _load_usage()
        self._file_idx = self._usage.get("current_file_idx", 0) % len(AUTH_FILES)
        self._account_id, self._gateway_id, self._token_pool = _load_auth(
            AUTH_FILES[self._file_idx]
        )
        self._token_idx = 0

    def _advance_auth_file(self):
        """Switch to the next auth file in the cycle and reset token index."""
        old_name = f"slot {self._file_idx + 1}"
        self._file_idx = (self._file_idx + 1) % len(AUTH_FILES)
        new_name = f"slot {self._file_idx + 1}"

        self._usage["current_file_idx"] = self._file_idx
        _save_usage(self._usage)

        self._account_id, self._gateway_id, self._token_pool = _load_auth(
            AUTH_FILES[self._file_idx]
        )
        self._token_idx = 0
        print(f"🔄 Summarize: neuron limit reached — switched {old_name} → {new_name}")

    def _add_neurons(self, model: str, prompt_tokens: int, completion_tokens: int):
        """Estimate neuron cost and add it to the running total for the active file."""
        factor  = _MODEL_NEURON_FACTOR.get(model, 0.05)
        neurons = (prompt_tokens + completion_tokens) * factor
        key     = str(self._file_idx)

        # Reload fresh usage to avoid overwriting parallel cog interactions
        current_usage = _load_usage()
        current_usage["neurons"][key] = current_usage["neurons"].get(key, 0.0) + neurons
        _save_usage(current_usage)

        total = current_usage["neurons"][key]
        print(
            f"🧠 Summarize: +{neurons:.1f} neurons  "
            f"(auth slot {self._file_idx + 1} total: {total:.1f})"
        )

        if total >= NEURON_SWITCH_THRESHOLD:
            self._advance_auth_file()

    @property
    def _api_url(self) -> str:
        return (
            f"https://gateway.ai.cloudflare.com/v1/"
            f"{self._account_id}/{self._gateway_id}/workers-ai/v1/chat/completions"
        )

    @property
    def _token(self) -> str:
        if not self._token_pool:
            return ""
        return self._token_pool[self._token_idx]

    def _rotate_token(self):
        if len(self._token_pool) <= 1:
            return
        old = self._token_idx
        self._token_idx = (self._token_idx + 1) % len(self._token_pool)
        print(
            f"🔑 Summarize: Rotated token slot {old + 1} → "
            f"{self._token_idx + 1} / {len(self._token_pool)}"
        )

    # ── core API call ─────────────────────────────────────────────────────────
    async def _call_cloudflare(
        self,
        prompt: str,
        model:  str = DEFAULT_MODEL,
        guild_id: int = None,
    ) -> str:
        """
        Sends a stateless prompt to Cloudflare AI Gateway.
        Pass guild_id so the personality reflects the bot's CURRENT
        per-guild nickname (falls back to the default nickname if omitted).
        Rotates API tokens on 401/403/429/404.
        Switches auth files when neurons exceed NEURON_SWITCH_THRESHOLD.
        """
        self._refresh_state()  # sync with shared state before each call

        if not self._token_pool:
            return "❌ No Cloudflare API tokens configured! Check the auth file."
        if not self._account_id or not self._gateway_id:
            return "❌ Missing account_id or gateway_id in the auth file!"

        messages = [
            {"role": "system", "content": await load_personality(self.bot, guild_id)},
            {"role": "user",   "content": prompt},
        ]

        payload = {
            "model":       model,
            "messages":    messages,
            "temperature": 0.6,
        }

        for _ in range(len(self._token_pool)):
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type":  "application/json",
            }
            try:
                async with self.bot.session.post(
                    self._api_url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=100),
                ) as resp:
                    if resp.status == 200:
                        data  = await resp.json()
                        usage = data.get("usage", {})
                        self._add_neurons(
                            model,
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                        )
                        return _clean_reply(data["choices"][0]["message"]["content"])

                    text = await resp.text()
                    print(
                        f"⚠️ Summarize token {self._token_idx + 1} — "
                        f"status {resp.status}: {text[:200]}"
                    )

                    if resp.status in _ROTATE_ON_STATUS:
                        self._rotate_token()
                        continue

                    return f"❌ AI Gateway returned error `{resp.status}` — try again later!"

            except asyncio.TimeoutError:
                return "⏳ Cloudflare AI took too long to respond — try again!"
            except Exception as e:
                print(f"⚠️ Summarize exception: {e}")
                return f"❌ Something went wrong: `{str(e)[:100]}`"

        return "❌ All API tokens exhausted! Check the active auth file."


    # ── Discord v10 REST message fetch ────────────────────────────────────────
    async def _fetch_messages_v10(
        self,
        channel_id: int,
        limit:      int,
        before_id:  int,
    ) -> list[dict]:
        """
        Fetch up to `limit` messages from a channel using the Discord v10 REST
        API, reading the bot token from auth/token.txt.

        Returns a list of normalised message dicts (oldest-first, bots excluded),
        each containing:
            display_name  str        — global_name or username
            content       str        — message text (may be empty if media-only)
            timestamp     str        — HH:MM formatted time
            cdn_urls      list[str]  — cdn.discordapp.com URLs for attachments
            embed_texts   list[str]  — readable text extracted from Discord embeds
            embed_media   list[str]  — image / thumbnail / video URLs from embeds
        """
        token = _load_discord_token()
        if not token:
            return []

        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        params = {
            "limit":  min(limit, 100),   # v10 hard-cap is 100
            "before": str(before_id),
        }
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type":  "application/json",
        }

        try:
            async with self.bot.session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(
                        f"⚠️ Summarize v10 fetch: status {resp.status} "
                        f"for channel {channel_id}: {body[:200]}"
                    )
                    return []

                raw: list[dict] = await resp.json()

        except asyncio.TimeoutError:
            print("⚠️ Summarize v10 fetch: timed out")
            return []
        except Exception as e:
            print(f"⚠️ Summarize v10 fetch: {e}")
            return []

        # Build normalised dicts (bots included), reverse to oldest-first
        messages = []
        for m in raw:
            text         = m.get("content", "").strip()
            is_bot       = bool(m.get("author", {}).get("bot"))
            # IS_FORWARDED flag = 1 << 14 = 16384
            is_forwarded = bool(m.get("flags", 0) & 16384)

            # ── Attachments → CDN URLs ────────────────────────────────────────
            cdn_urls: list[str] = [
                a["url"]
                for a in m.get("attachments", [])
                if "cdn.discordapp.com" in a.get("url", "")
            ]

            # ── Collect all embeds: direct + forwarded message snapshots ──────
            # Forwarded messages (flags & 16384) carry their original content
            # inside message_snapshots[N].message rather than at the top level.
            all_embeds: list[dict] = list(m.get("embeds", []))
            for snap in m.get("message_snapshots", []):
                snap_msg = snap.get("message", {})
                all_embeds.extend(snap_msg.get("embeds", []))
                # Pull CDN attachments from the snapshot too
                for a in snap_msg.get("attachments", []):
                    if "cdn.discordapp.com" in a.get("url", ""):
                        cdn_urls.append(a["url"])
                # Use snapshot text if the outer message is empty
                if not text and snap_msg.get("content", "").strip():
                    text = snap_msg["content"].strip()

            # ── Parse every embed (standard Discord rich + other types) ───────
            embed_texts: list[str] = []
            embed_media: list[str] = []

            def _parse_embed(e: dict):
                parts: list[str] = []
                # Embed type label (rich / image / video / link / article …)
                etype = e.get("type", "rich")
                if etype != "rich":
                    parts.append(f"Type: {etype}")
                # Embed author (e.g. "YouTube Subscriber Update")
                if e.get("author", {}).get("name"):
                    parts.append(f"From: {e['author']['name']}")
                # Title
                if e.get("title"):
                    parts.append(f"Title: {e['title']}")
                # Description (capped to avoid prompt bloat)
                if e.get("description"):
                    desc = e["description"]
                    parts.append(
                        f"Description: {desc[:300]}{'…' if len(desc) > 300 else ''}"
                    )
                # Fields (e.g. subscriber stats, durations …)
                for field in e.get("fields", []):
                    fname = field.get("name",  "").strip()
                    fval  = field.get("value", "").strip()
                    if fname or fval:
                        parts.append(f"{fname}: {fval}")
                # Main URL of the embed
                if e.get("url"):
                    parts.append(f"URL: {e['url']}")
                # Embed-level timestamp (different from message timestamp)
                if e.get("timestamp"):
                    parts.append(f"Date: {e['timestamp'][:10]}")
                # Footer text
                if e.get("footer", {}).get("text"):
                    parts.append(f"Footer: {e['footer']['text']}")
                if parts:
                    embed_texts.append("  |  ".join(parts))

                # Media: image / thumbnail / video
                for media_key in ("image", "thumbnail", "video"):
                    obj = e.get(media_key)
                    if obj and obj.get("url"):
                        embed_media.append(obj["url"])

            for e in all_embeds:
                _parse_embed(e)

            # Skip messages with absolutely no usable content
            if not text and not cdn_urls and not embed_texts and not embed_media:
                continue

            author  = m["author"]
            display = author.get("global_name") or author.get("username", "Unknown")
            if is_bot:
                display = f"🤖 {display}"
            ts_raw  = m.get("timestamp", "")
            ts_fmt  = ts_raw[11:16] if len(ts_raw) >= 16 else "??:??"

            messages.append({
                "display_name": display,
                "content":      text,
                "timestamp":    ts_fmt,
                "cdn_urls":     cdn_urls,
                "embed_texts":  embed_texts,
                "embed_media":  embed_media,
                "is_forwarded": is_forwarded,
                "is_bot":       is_bot,
            })

        messages.reverse()   # oldest first
        return messages

    # ── embed helpers ─────────────────────────────────────────────────────────
    async def _send_loading_embed(
        self,
        interaction: discord.Interaction,
        msg_count:   int,
    ) -> discord.WebhookMessage:
        """
        Send a pastel loading embed and return the message handle so it can
        be edited in-place once the AI finishes.
        """
        embed = discord.Embed(
            title       = "📝 Summarizing chat...",
            description = (
                f"Reading through **{msg_count}** messages and cooking up a summary! ✨\n\n"
                f"Hang tight... 💕"
            ),
            color = random.choice(PASTEL_COLORS),
        )
        embed.set_thumbnail(url=random.choice(LOADING_GIFS))
        return await interaction.followup.send(embed=embed)

    async def _edit_to_summary_embed(
        self,
        interaction:  discord.Interaction,
        loading_msg:  discord.WebhookMessage,
        summary:      str,
        msg_count:    int,
        model:        str,
        channel_name: str = "",
    ):
        """
        Edit the loading embed in-place with the finished summary.
        If the summary exceeds the embed description limit (4 096 chars),
        overflow is sent as plain follow-up messages.
        """
        EMBED_DESC_LIMIT = 4_096
        model_label      = _MODEL_LABELS.get(model, model)
        display_channel  = channel_name or interaction.channel.name

        embed = discord.Embed(
            title       = f"📝 Chat Summary — #{display_channel}",
            description = summary[:EMBED_DESC_LIMIT],
            color       = random.choice(PASTEL_COLORS),
            timestamp   = interaction.created_at.replace(tzinfo=timezone.utc),
        )
        embed.add_field(name="📨 Messages scanned", value=str(msg_count), inline=True)
        embed.add_field(name="🤖 Model",            value=model_label,    inline=True)
        embed.set_footer(
            text     = f"Requested by {interaction.user.display_name}",
            icon_url = interaction.user.display_avatar.url,
        )

        await loading_msg.edit(embed=embed)

        # Send any overflow as chunked plain follow-ups
        if len(summary) > EMBED_DESC_LIMIT:
            overflow = summary[EMBED_DESC_LIMIT:]
            chunks   = [overflow[i:i+1900] for i in range(0, len(overflow), 1900)]
            for chunk in chunks:
                await interaction.followup.send(chunk)

    # ── /summarize ────────────────────────────────────────────────────────────
    @app_commands.command(
        name        = "summarize",
        description = "📝 Read and beautifully summarize recent chat messages! 💕",
    )
    @app_commands.describe(
        limit   = "How many messages to look back at? (1–50, default 30) 🎀",
        model   = "Which AI model to use (default: Llama 3.1 8B Fast ⚡)",
        channel = "Channel to summarize (default: current channel) 📜",
    )
    @app_commands.choices(model=MODEL_CHOICES)
    async def summarize(
        self,
        interaction: discord.Interaction,
        limit:       app_commands.Range[int, 1, 50] = 30,
        model:       app_commands.Choice[str]       = None,
        channel:     discord.TextChannel            = None,
    ):
        await interaction.response.defer(thinking=True)

        selected_model  = model.value if model else DEFAULT_MODEL

        # Resolve target channel — user-picked or fall back to current
        target_channel    = channel or interaction.channel
        target_channel_id = target_channel.id
        target_name       = target_channel.name

        try:
            # ── Fetch chat logs via Discord v10 REST API ──────────────────────
            raw_history = await self._fetch_messages_v10(
                channel_id = target_channel_id,
                limit      = limit,
                before_id  = interaction.id,   # interaction snowflake ≈ "before now"
            )

            if not raw_history:
                await interaction.followup.send(
                    "🎀 There are no valid user messages to summarize in this range, silly!",
                    ephemeral=True,
                )
                return

            # ── Send loading embed immediately ────────────────────────────────
            loading_msg = await self._send_loading_embed(interaction, len(raw_history))

            def _format_msg(msg: dict) -> str:
                prefix = "↪️ [Forwarded] " if msg.get("is_forwarded") else ""
                body   = msg["content"] or ("(no text)" if msg.get("is_forwarded") else "")
                line   = f"[{msg['timestamp']}] {msg['display_name']}: {prefix}{body}"
                # Attachment CDN links
                for url in msg.get("cdn_urls", []):
                    line += f"\n  📎 {url}"
                # Embed text content (rich embeds, link previews, etc.)
                for etext in msg.get("embed_texts", []):
                    line += f"\n  🔗 [Embed] {etext}"
                # Embed media links (images / thumbnails / video)
                for murl in msg.get("embed_media", []):
                    line += f"\n  🖼️  {murl}"
                return line

            formatted_logs = [_format_msg(msg) for msg in raw_history]
            chat_payload_block = "\n".join(formatted_logs)

            # ── Build structured prompt ───────────────────────────────────────
            prompt = (
                f"You are given a transcript of the last {len(raw_history)} messages "
                f"from a Discord text channel. "
                f"Review the conversation and provide a concise, beautiful summary. "
                f"Identify the main topics discussed, note any decisions or highlights, "
                f"and capture the general mood. "
                f"Use bullet points where appropriate.\n\n"
                f"TRANSCRIPT:\n"
                f"```text\n"
                f"{chat_payload_block}\n"
                f"```"
            )

            # ── Call Cloudflare AI ────────────────────────────────────────────
            summary_reply = await self._call_cloudflare(
                prompt, model=selected_model, guild_id=interaction.guild_id
            )

            # ── Log & swap loading embed → summary embed ──────────────────────
            await bridge_log(
                interaction,
                "summarize",
                f"Summarized {len(raw_history)} messages from #{target_name} (limit: {limit}, model: {selected_model}).",
                summary_reply,
            )

            await self._edit_to_summary_embed(
                interaction,
                loading_msg  = loading_msg,
                summary      = summary_reply,
                msg_count    = len(raw_history),
                model        = selected_model,
                channel_name = target_name,
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permissions (`Read Message History`) to scan this channel!",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ **Error running summary:** `{str(e)[:100]}`",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(SummarizeCog(bot))
