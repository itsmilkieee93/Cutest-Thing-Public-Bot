"""🌸 GroqService orchestrator — extracted from groq_ai.py's
monolithic class. Structure-only split: __init__/rotate_key are moved
verbatim, and get_ai_response is thinned down to the ORCHESTRATOR
SHAPE (intent short-circuit → check_safety → pick model+Exa → build
system prompt → build recent_history window → run completion loop) —
every behavioral detail that used to live inline now lives in one of
the sibling engine/ modules, wired on below via composition (each
sibling function takes `self` explicitly and is exposed as a bound
method on GroqService, so nothing outside this package needs to know
the split happened).

groq_ai.py re-exports GroqService from here so `from groq_ai import
GroqService` keeps working unchanged for every existing caller/test.
"""
import os
import sys
import discord

from groq import Groq, AsyncGroq

# 🌸 key_config.py lives at auth/key_config.py, gitignored — see generate_key_config.py.
# Path is relative to CWD (bot root), matching this file's existing convention
# of using relative paths like "auth/groq_key" instead of __file__-based ones.
if "auth" not in sys.path:
    sys.path.insert(0, "auth")
import key_config

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

from engine import logging as _logging_mod
from engine import classifiers as _classifiers_mod
from engine import safety as _safety_mod
from engine import prompt as _prompt_mod
from engine import memory_window as _memory_window_mod
from engine import completion as _completion_mod
from engine.dm_notice import generate_dm_notice as _generate_dm_notice


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

    # 🌸 Square thumbnail shown in the corner of the log embeds. Kept as
    # class attributes for compatibility with any external code that
    # references GroqService.SUCCESS_THUMBNAIL_URL / FAIL_THUMBNAIL_URL
    # directly; the actual embed builders live in engine/logging.py.
    SUCCESS_THUMBNAIL_URL = _logging_mod.SUCCESS_THUMBNAIL_URL
    FAIL_THUMBNAIL_URL    = _logging_mod.FAIL_THUMBNAIL_URL

    def __init__(self, bot: "commands.Bot" = None):
        self.personality_path = "auth/personality.txt"

        # 🌸 Back-reference to the bot so 429 / success embeds can be fired
        # straight from get_ai_response via bot._broadcast_log_embed. May be
        # None (e.g. in a standalone script) — every call site guards for it.
        self.bot = bot

        self.api_keys          = list(key_config.GROQ_API_KEYS)
        self.current_key_index = 0

        if self.api_keys:
            # 🌸 self.client is SYNC (for modules using asyncio.to_thread)
            # self.async_client is ASYNC (for direct await calls within groq_ai.py)
            self.client = Groq(api_key=self.api_keys[self.current_key_index])
            self.async_client = AsyncGroq(api_key=self.api_keys[self.current_key_index])
        else:
            self.client = None
            self.async_client = None
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
        self.async_client = AsyncGroq(api_key=self.api_keys[self.current_key_index])
        print(f"🔄 Swapping to Groq API Key #{self.current_key_index + 1}...")

    # ------------------------------------------------------------------
    # 🌸 Logging / embeds — see engine/logging.py. Bound-method wrappers
    # so existing call sites (self._log_rate_limits(...), etc.) and any
    # external code patching GroqService methods keep working unchanged.
    # ------------------------------------------------------------------
    def _extract_ratelimit_headers(self, headers) -> dict:
        return _logging_mod._extract_ratelimit_headers(headers)

    def _parse_groq_429(self, error: Exception) -> dict:
        return _logging_mod._parse_groq_429(error)

    def _build_rate_limit_embed(self, error: Exception, username: str, user_id: int, model_id: str) -> discord.Embed:
        return _logging_mod._build_rate_limit_embed(self, error, username, user_id, model_id)

    def _build_success_embed(self, username: str, user_id: int, guild, channel, model_id: str, headers=None) -> discord.Embed:
        return _logging_mod._build_success_embed(self, username, user_id, guild, channel, model_id, headers=headers)

    def _log_ai_transcript(self, username: str, user_id: int, model_id: str,
                            guild, channel, prompt: str, reply: str):
        return _logging_mod._log_ai_transcript(username, user_id, model_id, guild, channel, prompt, reply)

    def _log_rate_limits(self, headers, username: str, model_id: str):
        return _logging_mod._log_rate_limits(self, headers, username, model_id)

    # ------------------------------------------------------------------
    # 🌸 Safety — see engine/safety.py.
    # ------------------------------------------------------------------
    async def check_safety(self, prompt: str, username: str) -> bool:
        return await _safety_mod.check_safety(self, prompt, username)

    async def check_output_safety(self, reply: str, username: str) -> bool:
        return await _safety_mod.check_output_safety(self, reply, username)

    async def _generate_safeguard_decline(self, username: str, display_name: str = None, guild=None, flagged_text: str = None) -> str:
        return await _safety_mod._generate_safeguard_decline(self, username, display_name, guild, flagged_text=flagged_text)

    # ------------------------------------------------------------------
    # 🌸 DM notice — see engine/dm_notice.py. Kept next to
    # extras/groq_dm_instruct.py's usage (route_dm_split calls this as
    # its ai_notice callback) per the refactor spec.
    # ------------------------------------------------------------------
    async def generate_dm_notice(self, message: discord.Message, outcome: str) -> str:
        return await _generate_dm_notice(self, message, outcome)

    # ------------------------------------------------------------------
    # 🌸 Classifiers — see engine/classifiers.py.
    # ------------------------------------------------------------------
    async def classify_server_query(self, message_content: str, username: str) -> str:
        return await _classifiers_mod.classify_server_query(self, message_content, username)

    async def classify_search_intent(self, prompt: str, username: str) -> bool:
        return await _classifiers_mod.classify_search_intent(self, prompt, username)

    async def classify_user_intent(self, user_message: str) -> str:
        return await _classifiers_mod.classify_user_intent(self, user_message)

    # ------------------------------------------------------------------
    # 🌸 get_ai_response — ORCHESTRATOR SHAPE:
    #   classify_user_intent QUERY_STAFF short-circuit
    #   → check_safety → decline
    #   → pick model + optional Exa            (engine/prompt.py)
    #   → build system prompt                  (engine/prompt.py)
    #   → build recent_history window          (engine/memory_window.py)
    #   → run completion loop                  (engine/completion.py)
    #     (check_output_safety, memory save, transcript + success embed
    #      all live INSIDE the completion loop — see contract notes in
    #      engine/completion.py; get_ai_response never inlines them.)
    # ------------------------------------------------------------------
    async def get_ai_response(self, prompt: str, username: str, user_id: int, display_name: str = None, global_name: str = None, guild_nickname: str = None, model_id: str = None, react_allowed: bool = False, dm_requested: bool = False, guild=None, channel=None, recent_react_emoji: list[str] = None, shared=None, message_id: int = None, reply_to_message_id: int = None, reply_to_message_text: str = None, tone_hint: str = "") -> str | None:
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
        grabbing the newest tail — see engine/memory_window.py for
        details.

        🌸 FALLBACK CONTEXT: `reply_to_message_text` is the ACTUAL text/
        embed content of that replied-to bot message, extracted straight
        from Discord by bot_service._reply_to_bot_context — independent
        of whether it's in Groq's memory at all. Needed because interceptor
        replies (avatar/owner/server-info/media/etc. in handle_mention_reaction)
        never go through get_ai_response, so they never get saved to
        history and can never be found by the anchor search. When
        that search comes up empty but reply_to_message_text is non-empty,
        that raw text gets injected directly into the prompt instead —
        see engine/memory_window.py's FALLBACK CONTEXT block.

        🌸 IDENTITY FIELDS: `global_name` (message.author.global_name —
        the account-wide display name from User Settings, may be None)
        and `guild_nickname` (message.author.nick — THIS server's
        nickname override, always None in a DM) are passed separately
        from `display_name` because display_name is discord.py's already
        -collapsed nick→global_name→username fallback chain and can't be
        un-collapsed after the fact. All three plus the snowflake-decoded
        creation date go into IDENTITY_INSTRUCTIONS (see groq_instruct.py)
        so the AI can answer identity questions ("what's my snowflake
        id", "when was my account made", "what's my username vs my
        nickname here") straight from context, no tool/decoder site
        needed.

        🌸 `tone_hint` — optional, from extras/emotion_detector.py's
        get_tone_hint(). Empty string (default) means no mood was
        detected worth nudging for, so personality-building below is
        byte-for-byte the same as before this param existed. When
        non-empty, it's appended to this turn's personality instructions
        ONLY — never saved to history, never affects any other user's
        reply, and (per emotion_detector's contract) is either a light
        tone nudge (upset/stressed/angry/excited) or, for a detected
        crisis, a full override telling the model to drop the slang
        persona entirely and respond with genuine care instead.
        """
        if not self.client:
            return None

        # 🌸 INTENT-CLASSIFIER INTERCEPTOR — runs BEFORE check_safety and
        # every other expensive step below (model pick, history load,
        # the actual chat completion). If this cheap classifier call
        # confidently detects a QUERY_STAFF ask ("who are the mods",
        # "who's staff here"), answer it straight from the local
        # roles.db cache and return immediately — no main Groq chat
        # generation, no history round-trip, no wasted tokens.
        #
        # Guild-only: staff/mod roles only exist in a guild context —
        # `guild` is None in DMs, so this is skipped there entirely and
        # falls through to normal chat, same as every other
        # guild-scoped interceptor in groq_service.py.
        #
        # get_staff_context_string returning "" (no cached roles.db yet,
        # nobody actually holds either role, or a query error) is
        # treated as "nothing to intercept with" — falls through to the
        # normal chat path below rather than replying with a blank
        # message, same fail-open shape as classify_user_intent itself.
        #
        # 🌸 NOTE ON QUERY_ROLE: classify_user_intent's label pool
        # includes QUERY_ROLE (see its docstring), but this interceptor
        # deliberately does NOT act on it. groq_service.py's on_message
        # flow already has a separate, pre-existing classifier
        # (classify_server_query) whose "role_query"/"role_list" labels
        # dispatch to handle_role_query/handle_role_list_query — running
        # a SECOND independent classification here for the same intent
        # would mean two AI calls racing to decide the same thing, on
        # two different label vocabularies, with no shared source of
        # truth. QUERY_ROLE stays reserved on the label list (so future
        # callers of classify_user_intent that don't go through
        # groq_service.py's flow can still detect it), but the actual
        # role-query routing lives in exactly one place: groq_service.py.
        if guild is not None:
            intent_label = await self.classify_user_intent(prompt)
            if intent_label == "QUERY_STAFF":
                staff_context = await shared.get_staff_context_string(guild.id) if shared else ""
                if staff_context:
                    return f"Here's who's on staff right now! 🌸\n\n{staff_context}"
                # 🌸 No cached staff data — fall through to normal chat
                # rather than dead-ending on an empty interceptor reply.

        if not await self.check_safety(prompt, username):
            return await self._generate_safeguard_decline(username, display_name, guild, flagged_text=prompt)

        # 🌸 Pick model (+ optional Exa search) — see engine/prompt.py
        # for the full explicit-model / classify_search_intent /
        # _wants_web_search / Exa-first order this preserves.
        model_to_use, exa_context, is_compound_call = await _prompt_mod.pick_model_and_search(
            self, prompt, username, model_id,
        )

        # 🌸 Build the full system prompt (personality, react/DM/identity
        # directives, server context, Exa grounding, anchor instruction)
        # — see engine/prompt.py.
        personality = await _prompt_mod.build_system_prompt(
            self,
            prompt=prompt,
            username=username,
            user_id=user_id,
            display_name=display_name,
            global_name=global_name,
            guild_nickname=guild_nickname,
            react_allowed=react_allowed,
            dm_requested=dm_requested,
            guild=guild,
            recent_react_emoji=recent_react_emoji,
            is_compound_call=is_compound_call,
            exa_context=exa_context,
            reply_to_message_id=reply_to_message_id,
            tone_hint=tone_hint,
        )

        # 🌸 Load full history + build this call's recent_history window
        # (random-sized, reply-anchored, interceptor-fallback-aware) —
        # see engine/memory_window.py.
        history, recent_history, guild_id, dm_channel_id = await _memory_window_mod.load_history_and_window(
            user_id=user_id,
            guild=guild,
            channel=channel,
            is_compound_call=is_compound_call,
            reply_to_message_id=reply_to_message_id,
            reply_to_message_text=reply_to_message_text,
        )

        # 🌸 Run the actual completion (kwargs-per-model-family + the
        # 413/429 retry chain), including output-safety check, transcript
        # logging, memory save, and the success embed — see
        # engine/completion.py.
        return await _completion_mod.run_completion_loop(
            self,
            prompt=prompt,
            personality=personality,
            recent_history=recent_history,
            model_to_use=model_to_use,
            is_compound_call=is_compound_call,
            username=username,
            user_id=user_id,
            guild=guild,
            channel=channel,
            display_name=display_name,
            history=history,
            message_id=message_id,
            guild_id=guild_id,
            dm_channel_id=dm_channel_id,
        )
