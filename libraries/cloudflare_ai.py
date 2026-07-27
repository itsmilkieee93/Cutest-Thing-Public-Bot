"""
☁️ cloudflare_ai.py
Discord cog for /cloudflare-ai and /cloudflare-ai-reply slash commands
Uses Cloudflare AI Gateway (OpenAI-compatible endpoint, aiohttp, async)

Auth files (cycled automatically when neurons > NEURON_SWITCH_THRESHOLD):
  auth/cloudflare_ai        ← default / first
  auth/cloudflare_ai_2      ← second
  auth/cloudflare_ai_3      ← third   … loops back to first

Each auth file format (one per line):
  Line 1: Cloudflare Account ID
  Line 2: Gateway ID
  Line 3+: API tokens (rotated on 401 / 403 / 429 / 404)

Neuron usage is persisted to auth/cloudflare_usage.json.
When the running total for the active auth file exceeds NEURON_SWITCH_THRESHOLD
the cog advances to the next auth file (wrapping around) and continues.

Personality: dynamic, built live from the bot's CURRENT per-guild nickname
(set via /server-persona-set) — see personality.py. Same system groq_ai.py uses.
Memory stored per-user at gemini/memory/cloudflare_ai/chat_{user}_{id}.json
"""

import re
import json
import base64
import urllib.parse
import discord
import aiohttp
import asyncio
import os
from discord import app_commands
from discord.ext import commands
from resources.shared import (
    load_gemini_memory, save_gemini_memory, bridge_log,
    reply_to_autocomplete,
)

# 🌸 Dynamic personality — instructions are generated live from the bot's
# CURRENT per-guild nickname (set via /server-persona-set), same system
# groq_ai.py uses, instead of a static personality.txt file.
from personality import load_personality

# ─────────────────────────────────────────────────────────────────────────────
# Paths & constants
# ─────────────────────────────────────────────────────────────────────────────
_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
_AUTH_DIR    = os.path.join(_BASE_DIR, "..", "auth")

import sys
if _AUTH_DIR not in sys.path:
    sys.path.insert(0, _AUTH_DIR)
import key_config  # lives at auth/key_config.py, gitignored — see generate_key_config.py

# Each entry is a dict: {"account_id": ..., "gateway_id": ..., "tokens": [...]}
CLOUDFLARE_AUTH_SETS = [
    key_config.CLOUDFLARE_AI,
    key_config.CLOUDFLARE_AI_2,
    key_config.CLOUDFLARE_AI_3,
]
AUTH_FILES = CLOUDFLARE_AUTH_SETS  # kept as alias so len(AUTH_FILES) usages below still work

USAGE_PATH        = os.path.join(_AUTH_DIR, "cloudflare_usage.json")

MEMORY_MODEL_ID         = "cloudflare_ai"
MAX_HISTORY             = 200
NEURON_SWITCH_THRESHOLD = 10_000   # switch auth file after this many neurons

# Errors that trigger an API token rotation within a single auth file
_ROTATE_ON_STATUS = {401, 403, 429, 404}

# ─────────────────────────────────────────────────────────────────────────────
# Model catalogue
# ─────────────────────────────────────────────────────────────────────────────
# Neuron-per-token factor used to estimate neuron cost from the usage block
# returned by the API  (neurons ≈ total_tokens × factor).
# Derived from the ranges in the model tier list.
_MODEL_NEURON_FACTOR: dict[str, float] = {
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast": 0.033,
    "@cf/meta/llama-3.2-11b-vision-instruct":  0.048,
    "@cf/meta/llama-3.1-70b-instruct-fp8-fast": 0.080,
    "@cf/qwen/qwen3-30b-a3b-fp8":              0.072,
    "@cf/google/gemma-4-26b-a4b-it":           0.085,
}

DEFAULT_MODEL  = "@cf/meta/llama-3.1-8b-instruct-fp8-fast"
VISION_MODEL   = "@cf/meta/llama-3.2-11b-vision-instruct"

# MIME types we'll attempt to send as vision input
_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png",
    "image/gif",  "image/webp",
}
# Extensions → MIME fallback when Discord doesn't set content_type
_EXT_TO_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",  ".gif":  "image/gif",
    ".webp": "image/webp",
}
# Video extensions we recognise (Cloudflare can't decode video frames directly)
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}

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
        print(f"⚠️ CloudflareAI: incomplete auth set in key_config.py "
              f"(need account_id, gateway_id, and at least one token)")
        return "", "", []

    return account_id, gateway_id, tokens


# ─────────────────────────────────────────────────────────────────────────────
# Neuron usage persistence
# ─────────────────────────────────────────────────────────────────────────────
def _load_usage() -> dict:
    """
    Load usage JSON.  Schema:
      {
        "current_file_idx": 0,
        "neurons": {"0": 1234.5, "1": 0.0, "2": 0.0}
      }
    """
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
        print(f"⚠️ CloudflareAI: Could not save usage: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Google Suggestions helper  (no API key required ✨)
# ─────────────────────────────────────────────────────────────────────────────
_SUGGEST_URL         = "https://suggestqueries.google.com/complete/search"
_SUGGEST_USER_AGENT  = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_SUGGEST_MAX         = 10   # how many suggestions to inject as context


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────
class CloudflareAICog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Mutable usage state (kept in memory, flushed to disk after every call)
        self._usage = _load_usage()
        self._file_idx: int = self._usage.get("current_file_idx", 0) % len(AUTH_FILES)

        # Load the active auth file
        self._account_id, self._gateway_id, self._token_pool = _load_auth(
            AUTH_FILES[self._file_idx]
        )
        self._token_idx = 0

        self._log_status()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _log_status(self):
        auth_name = f"CLOUDFLARE_AI slot {self._file_idx + 1}"
        neurons   = self._usage["neurons"].get(str(self._file_idx), 0.0)
        print(
            f"☁️  CloudflareAI active auth: {auth_name}  "
            f"(slot {self._file_idx + 1}/{len(AUTH_FILES)}, "
            f"~{neurons:.1f} neurons used)"
        )

    def _advance_auth_file(self):
        """Switch to the next auth file in the cycle and reset that file's token idx."""
        old_name = f"slot {self._file_idx + 1}"
        self._file_idx = (self._file_idx + 1) % len(AUTH_FILES)
        new_name = f"slot {self._file_idx + 1}"

        self._usage["current_file_idx"] = self._file_idx
        _save_usage(self._usage)

        self._account_id, self._gateway_id, self._token_pool = _load_auth(
            AUTH_FILES[self._file_idx]
        )
        self._token_idx = 0
        print(
            f"🔄 CloudflareAI: neuron limit reached — "
            f"switched {old_name} → {new_name}"
        )

    def _add_neurons(self, model: str, prompt_tokens: int, completion_tokens: int):
        """Estimate neuron cost and add it to the running total for the active file."""
        factor   = _MODEL_NEURON_FACTOR.get(model, 0.05)
        neurons  = (prompt_tokens + completion_tokens) * factor
        key      = str(self._file_idx)
        self._usage["neurons"][key] = self._usage["neurons"].get(key, 0.0) + neurons
        _save_usage(self._usage)

        total = self._usage["neurons"][key]
        print(
            f"🧠 CloudflareAI: +{neurons:.1f} neurons  "
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
            f"🔑 CloudflareAI: Rotated token slot {old + 1} → "
            f"{self._token_idx + 1} / {len(self._token_pool)}"
        )

    # ── core API call ─────────────────────────────────────────────────────────
    async def _call_cloudflare(
        self,
        prompt: str,
        history: list,
        model: str = DEFAULT_MODEL,
        image_b64: str = None,
        image_mime: str = None,
        top_p: float = None,
        top_k: int   = None,
        guild_id: int = None,
    ) -> str:
        """
        Sends full conversation history + new prompt to Cloudflare AI Gateway.
        Pass image_b64 + image_mime to use vision-format messages.
        Pass top_p (0.0–1.0) and/or top_k (1–100) to tune sampling.
        Pass guild_id so the personality reflects the bot's CURRENT
        per-guild nickname (falls back to the default nickname if omitted).
        Rotates API tokens on 401/403/429/404.
        Switches auth files when neurons exceed NEURON_SWITCH_THRESHOLD.
        """
        if not self._token_pool:
            return "❌ No Cloudflare API tokens configured! Check the auth file."
        if not self._account_id or not self._gateway_id:
            return "❌ Missing account_id or gateway_id in the auth file!"

        personality = await load_personality(self.bot, guild_id)

        messages = [{"role": "system", "content": personality}]
        for entry in history:
            role    = entry.get("role", "user")
            content = entry.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        # Build the final user message — vision or plain text
        if image_b64 and image_mime:
            user_content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_mime};base64,{image_b64}"
                    },
                },
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": prompt})

        payload = {
            "model":       model,
            "messages":    messages,
            "temperature": 0.7,
        }
        if top_p is not None:
            payload["top_p"] = round(max(0.0, min(1.0, top_p)), 3)
        if top_k is not None:
            payload["top_k"] = max(1, min(100, top_k))

        # Try every token in the pool before giving up
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
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data   = await resp.json()
                        reply  = data["choices"][0]["message"]["content"]
                        usage  = data.get("usage", {})
                        self._add_neurons(
                            model,
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                        )
                        return _clean_reply(reply)

                    text = await resp.text()
                    print(
                        f"⚠️ CloudflareAI token {self._token_idx + 1} — "
                        f"status {resp.status}: {text[:200]}"
                    )

                    if resp.status in _ROTATE_ON_STATUS:
                        self._rotate_token()
                        continue

                    return f"❌ Cloudflare AI returned error `{resp.status}` — try again later!"

            except asyncio.TimeoutError:
                return "⏳ Cloudflare AI took too long to respond — try again!"
            except Exception as e:
                print(f"⚠️ CloudflareAI exception: {e}")
                return f"❌ Something went wrong: `{str(e)[:100]}`"

        return "❌ All API tokens exhausted! Check the active auth file."

    # ── web search helper ─────────────────────────────────────────────────────
    async def _google_search(self, query: str, search_type: str = "web") -> str:
        """
        Fetches real-time Google autocomplete suggestions (no API key needed).
        search_type: 'web' for Google, 'youtube' for YouTube-specific suggestions.
        Returns a formatted context string to inject into the prompt.
        """
        encoded = urllib.parse.quote_plus(query)
        url     = f"{_SUGGEST_URL}?client=firefox&q={encoded}"
        if search_type == "youtube":
            url += "&ds=yt"

        headers = {"User-Agent": _SUGGEST_USER_AGENT}

        try:
            async with self.bot.session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    print(f"⚠️ CloudflareAI Google Suggest: status {resp.status}")
                    return ""

                data        = await resp.json(content_type=None)
                suggestions = data[1][:_SUGGEST_MAX] if data and len(data) > 1 else []

                if not suggestions:
                    return "🔍 *No web suggestions found for this query.*"

                label  = "🎬 YouTube" if search_type == "youtube" else "🌐 Google"
                lines  = [f"{label} **Search Suggestions for** `{query}`:"]
                lines += [f"  {i}. {s}" for i, s in enumerate(suggestions, 1)]
                return "\n".join(lines)

        except asyncio.TimeoutError:
            print("⚠️ CloudflareAI Google Suggest: timed out")
            return ""
        except Exception as e:
            print(f"⚠️ CloudflareAI Google Suggest error: {e}")
            return ""

    # ── media helper ──────────────────────────────────────────────────────────
    async def _fetch_media_as_base64(
        self, attachment: discord.Attachment
    ) -> tuple[str, str] | None:
        """
        Download a Discord attachment and return (base64_str, mime_type).
        Returns None if the file is not a supported image type.
        """
        import os as _os
        ext  = _os.path.splitext(attachment.filename.lower())[1]
        mime = (attachment.content_type or "").split(";")[0].strip().lower()

        # Resolve MIME from extension if Discord didn't provide one
        if not mime:
            mime = _EXT_TO_MIME.get(ext, "")

        if mime not in _IMAGE_MIMES:
            return None  # unsupported (video, audio, etc.)

        try:
            async with self.bot.session.get(
                attachment.url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
                return base64.b64encode(raw).decode(), mime
        except Exception as e:
            print(f"⚠️ CloudflareAI: failed to fetch attachment — {e}")
            return None

    # ── chunked send ──────────────────────────────────────────────────────────
    async def _send_chunked(self, interaction: discord.Interaction, reply: str, ephemeral: bool = False):
        user = interaction.user
        if len(reply) > 2000:
            chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
            await interaction.followup.send(f"{user.mention}\n{chunks[0]}", ephemeral=ephemeral)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=ephemeral)
        else:
            await interaction.followup.send(f"{user.mention}\n\n{reply}", ephemeral=ephemeral)

    # ── /cloudflare-ai ────────────────────────────────────────────────────────
    @app_commands.command(
        name="cloudflare-ai",
        description="☁️ Chat with an AI model via Cloudflare AI Gateway! 🧠",
    )
    @app_commands.describe(
        prompt      = "What do you want to ask? ✨",
        image       = "Attach an image or photo for the AI to see 🖼️ (auto-selects Vision model)",
        web_search  = "Fetch Google/YouTube suggestions and inject as context 🔍 (no API key!)",
        search_type = "Where to pull suggestions from (default: Google web 🌐)",
        top_p       = "Nucleus sampling threshold 0.0–1.0 (default: off) 🎲",
        top_k       = "Top-K sampling 1–100 (default: off) 🎯",
        model       = "Which AI model to use (default: Llama 3.1 8B Fast ⚡)",
    )
    @app_commands.choices(
        model=MODEL_CHOICES,
        search_type=[
            app_commands.Choice(name="🌐 Google Web",  value="web"),
            app_commands.Choice(name="🎬 YouTube",     value="youtube"),
        ],
    )
    async def cloudflare_ai(
        self,
        interaction: discord.Interaction,
        prompt:      str,
        image:       discord.Attachment = None,
        web_search:  bool = False,
        search_type: app_commands.Choice[str] = None,
        top_p:       app_commands.Range[float, 0.0, 1.0] = None,
        top_k:       app_commands.Range[int,   1,   100] = None,
        model:       app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        selected_model  = model.value if model else DEFAULT_MODEL
        stype           = search_type.value if search_type else "web"

        try:
            # ── resolve image attachment ──────────────────────────────────────
            image_b64, image_mime = None, None
            if image:
                import os as _os
                ext = _os.path.splitext(image.filename.lower())[1]
                if ext in _VIDEO_EXTS:
                    await interaction.followup.send(
                        "⚠️ Video files aren't supported for vision — "
                        "please attach an image (JPG, PNG, GIF, WebP) instead!",
                        ephemeral=True,
                    )
                    return
                result = await self._fetch_media_as_base64(image)
                if result is None:
                    await interaction.followup.send(
                        "⚠️ Couldn't read that file as an image. "
                        "Supported types: JPG, PNG, GIF, WebP.",
                        ephemeral=True,
                    )
                    return
                image_b64, image_mime = result
                if not model:
                    selected_model = VISION_MODEL

            # ── optional web search ───────────────────────────────────────────
            search_context = ""
            if web_search:
                search_context = await self._google_search(prompt, stype)

            final_prompt = prompt
            if search_context:
                final_prompt = (
                    f"{search_context}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Using the suggestions above as additional context, answer this:\n{prompt}"
                )

            user    = interaction.user
            history = load_gemini_memory(MEMORY_MODEL_ID, user.name, user.id)
            history.append({"role": "user", "content": prompt})

            reply = await self._call_cloudflare(
                final_prompt, history[:-1], selected_model,
                image_b64=image_b64, image_mime=image_mime,
                top_p=top_p, top_k=top_k,
                guild_id=interaction.guild_id,
            )

            history.append({"role": "assistant", "content": reply})
            save_gemini_memory(MEMORY_MODEL_ID, user.name, user.id, history[-MAX_HISTORY:])

            await bridge_log(interaction, "cloudflare-ai", prompt, reply)
            await self._send_chunked(interaction, reply, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(
                f"⚠️ **Error:** `{str(e)[:100]}`", ephemeral=True
            )

    # ── /cloudflare-ai-reply ──────────────────────────────────────────────────
    @app_commands.command(
        name="cloudflare-ai-reply",
        description="☁️ Make Cloudflare AI reply to a specific message! 🎯",
    )
    @app_commands.describe(
        channel     = "Pick the channel the message is in 📢",
        message_id  = "Pick a message to reply to ✨",
        instruction = "What should the AI do with it? (optional)",
        web_search  = "Fetch Google/YouTube suggestions for the message content 🔍 (no API key!)",
        search_type = "Where to pull suggestions from (default: Google web 🌐)",
        top_p       = "Nucleus sampling threshold 0.0–1.0 (default: off) 🎲",
        top_k       = "Top-K sampling 1–100 (default: off) 🎯",
        model       = "Which AI model to use (default: Llama 3.1 8B Fast ⚡)",
    )
    @app_commands.autocomplete(message_id=reply_to_autocomplete)
    @app_commands.choices(
        model=MODEL_CHOICES,
        search_type=[
            app_commands.Choice(name="🌐 Google Web",  value="web"),
            app_commands.Choice(name="🎬 YouTube",     value="youtube"),
        ],
    )
    async def cloudflare_ai_reply(
        self,
        interaction: discord.Interaction,
        channel:     discord.TextChannel = None,
        message_id:  str = None,
        instruction: str = "Reply naturally.",
        web_search:  bool = False,
        search_type: app_commands.Choice[str] = None,
        top_p:       app_commands.Range[float, 0.0, 1.0] = None,
        top_k:       app_commands.Range[int,   1,   100] = None,
        model:       app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        selected_model = model.value if model else DEFAULT_MODEL
        stype          = search_type.value if search_type else "web"

        try:
            lookup_channel = channel or interaction.channel

            if not lookup_channel:
                await interaction.followup.send("❌ Channel not found!", ephemeral=True)
                return

            if not message_id or not message_id.isdigit():
                await interaction.followup.send(
                    "⚠️ Valid Message ID required.", ephemeral=True
                )
                return

            target_msg = await lookup_channel.fetch_message(int(message_id))

            # ── detect media in the target message ────────────────────────────
            import os as _os
            image_b64, image_mime = None, None
            video_noted           = False

            for att in target_msg.attachments:
                ext = _os.path.splitext(att.filename.lower())[1]

                if ext in _VIDEO_EXTS:
                    # Can't decode video — note it in the prompt instead
                    video_noted = True
                    continue

                result = await self._fetch_media_as_base64(att)
                if result:
                    image_b64, image_mime = result
                    break  # use the first valid image

            # Auto-switch to vision model when media is present
            if image_b64 and not model:
                selected_model = VISION_MODEL

            personality = await load_personality(self.bot, interaction.guild_id)
            video_note  = " [Note: the message also contained a video file that cannot be analysed.]" if video_noted else ""

            # ── optional web search on the message content ────────────────────
            search_context = ""
            if web_search and target_msg.content:
                search_context = await self._google_search(target_msg.content, stype)

            search_note = f"\n\nWEB SEARCH CONTEXT:\n{search_context}" if search_context else ""

            prompt = (
                f"SYSTEM_INSTRUCTIONS:\n{personality}\n\n"
                f"CONTEXT: Replying to {target_msg.author.display_name}: "
                f"\"{target_msg.content}\"{video_note}{search_note}\n"
                f"USER DIRECTION: {instruction}"
            )

            # Memory disabled for /cloudflare-ai-reply — each reply is stateless 🚫🧠
            reply = await self._call_cloudflare(
                prompt, [], selected_model,
                image_b64=image_b64, image_mime=image_mime,
                top_p=top_p, top_k=top_k,
                guild_id=interaction.guild_id,
            )

            await bridge_log(
                interaction, "cloudflare-ai-reply",
                f"ID: {message_id} | Instr: {instruction}",
                reply,
            )

            if len(reply) > 2000:
                chunks = [reply[i:i+1950] for i in range(0, len(reply), 1950)]
                await target_msg.reply(chunks[0])
                for chunk in chunks[1:]:
                    await lookup_channel.send(chunk)
            else:
                await target_msg.reply(reply)

            await interaction.followup.send("✅ Replied!", ephemeral=True)

        except discord.NotFound:
            await interaction.followup.send("❌ Message not found!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error: `{str(e)[:100]}`", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(CloudflareAICog(bot))
