"""
🌸 openrouter.py
Discord cog for /openrouter and /openrouter-reply slash commands
Uses OpenRouter API (aiohttp, async) with free model tier
API key loaded from key_config.OPENROUTER_API_KEYS
Personality: dynamic, built live from the bot's CURRENT per-guild nickname
(set via /server-persona-set) — see personality.py. Same system groq_ai.py uses.
Memory stored per-user at gemini/memory/openrouter/chat_{user}_{id}.json
"""

import re
import discord
import aiohttp
import asyncio
import os
import sys
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

_AUTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auth")
if _AUTH_DIR not in sys.path:
    sys.path.insert(0, _AUTH_DIR)
import key_config  # lives at auth/key_config.py, gitignored — see generate_key_config.py

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL              = "openrouter/free"
MEMORY_MODEL_ID    = "openrouter"
MAX_HISTORY        = 200

# Errors that trigger an API key rotation
_ROTATE_ON_STATUS = {401, 403, 429, 404}

# Leaked chat-template tags some free models append to replies
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


def _build_key_pool() -> list:
    """Load the OpenRouter API key pool from key_config.OPENROUTER_API_KEYS."""
    keys = [k.strip() for k in (key_config.OPENROUTER_API_KEYS or []) if k.strip()]
    if not keys:
        print("⚠️ OpenRouter: key_config.OPENROUTER_API_KEYS is empty!")
    return keys


class OpenRouterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot       = bot
        self._key_pool = _build_key_pool()
        self._key_idx  = 0
        if not self._key_pool:
            print("⚠️ OpenRouter: No API keys found! Add keys to API_KEYS list.")

    @property
    def api_key(self) -> str:
        """Return the currently active API key."""
        if not self._key_pool:
            return ""
        return self._key_pool[self._key_idx]

    def _rotate_key(self):
        """Advance to the next key in the pool (wraps around)."""
        if len(self._key_pool) <= 1:
            return
        old_idx = self._key_idx
        self._key_idx = (self._key_idx + 1) % len(self._key_pool)
        print(f"🔑 OpenRouter: Rotated key slot {old_idx + 1} → {self._key_idx + 1} / {len(self._key_pool)}")

    # ─────────────────────────────────────────────────────────────────
    # Core API call
    # ─────────────────────────────────────────────────────────────────
    async def _call_openrouter(self, prompt: str, history: list, guild_id: int = None) -> str:
        """
        Sends full conversation history + new prompt to OpenRouter.
        Pass guild_id so the personality reflects the bot's CURRENT
        per-guild nickname (falls back to the default nickname if omitted).
        Automatically rotates to the next API key on 401 / 403 / 429 / 404.
        """
        if not self._key_pool:
            return "❌ No OpenRouter API keys configured! Add keys to `API_KEYS` in openrouter.py."

        personality = await load_personality(self.bot, guild_id)

        messages = [{"role": "system", "content": personality}]
        for entry in history:
            role    = entry.get("role", "user")
            content = entry.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model":       MODEL,
            "messages":    messages,
            "temperature": 0.7,
            "tools": [
                {
                    "type": "openrouter:web_search",
                    "parameters": {"max_results": 3}
                }
            ]
        }

        # Try every key in the pool before giving up
        for attempt in range(len(self._key_pool)):
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type":  "application/json",
            }
            try:
                async with self.bot.session.post(
                    OPENROUTER_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data  = await resp.json()
                        reply = data["choices"][0]["message"]["content"]
                        return _clean_reply(reply)

                    text = await resp.text()
                    print(f"⚠️ OpenRouter key {self._key_idx + 1} — status {resp.status}: {text[:200]}")

                    if resp.status in _ROTATE_ON_STATUS:
                        self._rotate_key()
                        continue  # retry with next key

                    # Non-rotatable error (e.g. 500) — bail immediately
                    return f"❌ OpenRouter returned error `{resp.status}` — try again later!"

            except asyncio.TimeoutError:
                return "⏳ OpenRouter took too long to respond — try again!"
            except Exception as e:
                print(f"⚠️ OpenRouter exception: {e}")
                return f"❌ Something went wrong: `{str(e)[:100]}`"

        return "❌ All API keys exhausted! Check your keys in `API_KEYS` inside openrouter.py."

    # ─────────────────────────────────────────────────────────────────
    # Helper: send reply chunked with user mention
    # ─────────────────────────────────────────────────────────────────
    async def _send_chunked(self, interaction: discord.Interaction, reply: str):
        user = interaction.user
        if len(reply) > 2000:
            chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
            await interaction.followup.send(f"{user.mention}\n{chunks[0]}")
            for chunk in chunks[1:]:
                await interaction.channel.send(chunk)
        else:
            await interaction.followup.send(f"{user.mention}\n{reply}")

    # ─────────────────────────────────────────────────────────────────
    # /openrouter
    # ─────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="openrouter",
        description="🤖 Chat with a free AI model via OpenRouter! 🧠"
    )
    @app_commands.describe(prompt="What do you want to ask? ✨")
    async def openrouter(self, interaction: discord.Interaction, prompt: str):
        await interaction.response.defer(thinking=True)

        try:
            user    = interaction.user
            history = load_gemini_memory(MEMORY_MODEL_ID, user.name, user.id)
            history.append({"role": "user", "content": prompt})

            reply = await self._call_openrouter(prompt, history[:-1], guild_id=interaction.guild_id)

            history.append({"role": "assistant", "content": reply})
            save_gemini_memory(MEMORY_MODEL_ID, user.name, user.id, history[-MAX_HISTORY:])

            await bridge_log(interaction, "openrouter", prompt, reply)
            await self._send_chunked(interaction, reply)

        except Exception as e:
            await interaction.followup.send(f"⚠️ **Error:** `{str(e)[:100]}`", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────
    # /openrouter-reply
    # ─────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="openrouter-reply",
        description="🤖 Make OpenRouter reply to a specific message! 🎯"
    )
    @app_commands.describe(
        channel    = "Pick the channel the message is in 📢",
        message_id = "Pick a message to reply to ✨",
        instruction= "What should the AI do with it? (optional)",
    )
    @app_commands.autocomplete(
        message_id=reply_to_autocomplete,
    )
    async def openrouter_reply(
        self,
        interaction: discord.Interaction,
        channel:     discord.TextChannel = None,
        message_id:  str = None,
        instruction: str = "Reply naturally.",
    ):
        await interaction.response.defer(thinking=True)

        try:
            # Resolve channel — use native TextChannel object or fallback
            lookup_channel = channel or interaction.channel

            if not lookup_channel:
                await interaction.followup.send("❌ Channel not found!", ephemeral=True)
                return

            if not message_id or not message_id.isdigit():
                await interaction.followup.send("⚠️ Valid Message ID required.", ephemeral=True)
                return

            # Fetch the target message
            target_msg = await lookup_channel.fetch_message(int(message_id))

            personality = await load_personality(self.bot, interaction.guild_id)
            prompt = (
                f"SYSTEM_INSTRUCTIONS:\n{personality}\n\n"
                f"CONTEXT: Replying to {target_msg.author.display_name}: \"{target_msg.content}\"\n"
                f"USER DIRECTION: {instruction}"
            )

            user    = interaction.user
            history = load_gemini_memory(MEMORY_MODEL_ID, user.name, user.id)
            history.append({"role": "user", "content": prompt})

            reply = await self._call_openrouter(prompt, history[:-1], guild_id=interaction.guild_id)

            history.append({"role": "assistant", "content": reply})
            save_gemini_memory(MEMORY_MODEL_ID, user.name, user.id, history[-MAX_HISTORY:])

            await bridge_log(
                interaction, "openrouter-reply",
                f"ID: {message_id} | Instr: {instruction}",
                reply,
            )

            # Native reply to the target message, chunked if needed
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
            await interaction.followup.send(f"❌ Error: `{str(e)[:100]}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OpenRouterCog(bot))
