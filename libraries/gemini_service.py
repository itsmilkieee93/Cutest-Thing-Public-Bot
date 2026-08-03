"""
🌸 gemini_service.py
GeminiService — wraps the Google Gemini API with API-key rotation and
dynamic per-guild personality (see personality.py).

Extracted out of bot_service.py so Gemini logic lives in its own file,
same as GroqService (groq_ai.py) and RouletteService (roulette.py).
"""

import time
import random
from google import genai
from google.genai import types

import key_config  # already on sys.path via bot_service.py's auth/ insert
from personality import get_personality_for_nickname


class GeminiService:
    def __init__(self, bot=None):
        # 🌸 Back-reference to the bot so we can resolve the CURRENT
        # per-guild nickname for dynamic personality (see personality.py).
        # May be None if constructed standalone — get_ai_response falls
        # back to the default nickname in that case.
        self.bot = bot

        self.api_keys          = list(key_config.GEMINI_API_KEYS)
        self.current_key_index = 0

        if self.api_keys:
            self.client = genai.Client(api_key=self.api_keys[self.current_key_index])
        else:
            print("❌ ERROR: No API keys found in key_config.GEMINI_API_KEYS!")

        self.default_model = "gemma-4-26b-a4b-it"
        print(f"🌸 Cutest Thing is online with {len(self.api_keys)} hearts (API Keys)!")

    # 🌸 Thinking-mode compatibility per model.
    # Not every model on the /gemini command exposes a thinking budget —
    # the fast Gemma 4 26B A4B (MoE) build doesn't support it, the dense
    # Gemma 4 31B does, and the Gemini Flash-Lite tiers don't expose it
    # either. Keep this map updated as new models get added to MODEL_CHOICES.
    THINKING_CAPABLE_MODELS = {
        "gemma-4-26b-a4b-it":    False,
        "gemma-4-31b-it":        True,
        "gemini-3.1-flash-lite": False,
        "gemini-3.5-flash-lite": False,
    }

    @classmethod
    def model_supports_thinking(cls, model_id: str) -> bool:
        """🌸 Whether the given model_id supports a thinking_config.
        Unknown models default to False so we never send an unsupported
        param and blow up the request."""
        return cls.THINKING_CAPABLE_MODELS.get(model_id, False)

    def rotate_key(self):
        if len(self.api_keys) <= 1:
            time.sleep(random.uniform(5, 12))
            return

        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        new_key = self.api_keys[self.current_key_index]
        self.client = genai.Client(api_key=new_key)
        print(f"🔄 Swapping to API Key #{self.current_key_index + 1}...")
        time.sleep(random.uniform(5, 12))

    def get_ai_response(
        self,
        prompt: str,
        guild_id: int = None,
        model_id: str = None,
        personality_override: str = None,
        enable_thinking: bool = False,
        disable_personality: bool = False,
    ):
        model_to_use = model_id or self.default_model

        # 🌸 disable_personality wins outright — no system_instruction at all,
        # for when someone wants a raw/neutral response with zero character.
        if disable_personality:
            personality = None
        # 🌸 Resolve the bot's CURRENT nickname in guild_id (if we have a
        # bot reference and a guild) and build personality instructions
        # from it live — same dynamic system groq_ai.py uses. Falls back
        # to the default nickname if bot/guild_id is missing or the bot
        # has no per-guild nickname set yet.
        elif personality_override and personality_override.strip():
            personality = personality_override.strip()
        else:
            nickname = None
            if self.bot and guild_id:
                guild = self.bot.get_guild(guild_id)
                if guild and guild.me:
                    nickname = guild.me.nick
            personality = get_personality_for_nickname(nickname)

        config_kwargs = dict(
            tools=[{"google_search": {}}],
            temperature=0.7,
            top_p=0.96,
            top_k=60,
        )
        if personality:
            config_kwargs["system_instruction"] = personality

        # 🌸 Only attach a thinking_config when both the caller asked for it
        # AND the chosen model actually supports it — otherwise we'd send a
        # param the model rejects. Silently skipped when unsupported rather
        # than erroring, since the caller may not know per-model support.
        if enable_thinking and self.model_supports_thinking(model_to_use):
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                include_thoughts=False,
                thinking_budget=-1,  # 🌸 dynamic/auto budget
            )

        config = types.GenerateContentConfig(**config_kwargs)

        for attempt in range(len(self.api_keys)):
            try:
                full_content = prompt
                if "youtube.com" in prompt or "youtu.be" in prompt:
                    full_content = f"Please analyze this YouTube link: {prompt}"

                response = self.client.models.generate_content(
                    model=model_to_use,
                    contents=full_content,
                    config=config
                )
                return response.text

            except Exception as e:
                err = str(e).lower()
                if "429" in err or "quota" in err or "exhausted" in err:
                    print(f"⚠️ Limit reached on Key #{self.current_key_index + 1}. Waiting...")
                    time.sleep(5)
                    self.rotate_key()
                    continue
                else:
                    return f"Service Error: {err}"

        return "Noo!! All my energy cells are empty! 😭 Try again in a bit 🥲 "
