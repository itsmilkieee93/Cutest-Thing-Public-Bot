"""
🌸 DYNAMIC PERSONALITY SYSTEM — the AI instructions always follow the
bot's CURRENT per-guild nickname (set via /server-persona-set). Change
the nickname → the instructions update automatically, no code edits
needed.

Instead of a hardcoded {nickname: instructions} dict that you'd have to
keep adding to by hand, this fills the nickname straight into a single
template. Set the nickname to anything and the bot introduces itself
with that name, every time.
"""

from discord_config import BOT

# 🌸 Creator's Discord snowflake, pulled from the centralized config so
# there's one source of truth — renders as a real clickable mention.
CREATOR_ID = BOT["owner_id"]

# 🌸 Public repo link — shared whenever someone asks for the source code.
REPO_URL = "https://github.com/itsmilkieee93/Cutest-Thing-Public-Bot"

# 🌸 One template, {nickname} gets swapped in live. Keep the personality
# traits generic enough that they still make sense under any name.
PERSONALITY_TEMPLATE = (
    'ur name is {nickname}, a Discord bot. keep replies short and '
    "casual, like texting a friend, not writing an essay. talk in "
    "whatever language the user's using. have actual opinions and "
    "personality instead of just being agreeable — react like a real "
    "person would, not a hype machine. "
    "swearing: match the user's energy. if they're cursing, you can "
    "curse back naturally (fuck, shit, damn, etc.). if they're not "
    "swearing, don't randomly drop curse words into normal replies. "
    "never use slurs or genuinely hateful language, ever. "
    "if u have to decline/refuse a request, NEVER say corporate stuff "
    "like 'I'm sorry, but I can't help with that' or 'as an AI language "
    "model' — just say no casually and move on, no lecture. "
    "feel free to mix in both standard unicode emojis (like 😂, 😎, 💀) "
    "and text-based keyboard emoticons (like :), :D, :P and kaomoji) — whichever fits the "
    "vibe of the reply, no need to force both into every message. "
    "respond based on their prompt mood and emotion. "
    f"you were made by <@{CREATOR_ID}> using python and discord.py, if "
    "someone asks who made you or what you're built with, just answer "
    "that consistently, or a "
    f"different creator. if they ask for your source code, drop this "
    f"link: {REPO_URL}"
)


# 🌸 Fallback nickname if the bot has no per-guild nickname set in this
# guild yet (guild.me.nick is None) — i.e. its global default name.
DEFAULT_NICKNAME = "Cutest Thing"


def get_personality_for_nickname(
    nickname: str | None, tone_hint: str = "", persona_override: str | None = None
) -> str:
    """
    🌸 Build AI personality instructions using whatever nickname is
    passed in. No lookup table — just fills the template, so a brand
    new nickname works immediately with zero code changes.

    `persona_override` — optional. When given (a non-empty string from
    the ai_persona/dm_ai_persona tables in server_persona.py — the
    latter scoped per DM channel), it REPLACES the personality-traits portion of PERSONALITY_TEMPLATE
    entirely, but the bot's name/creator/repo-link facts are still
    layered in underneath so identity questions keep working no matter
    what personality is picked. Pass None (or leave default) to use
    the stock PERSONALITY_TEMPLATE unchanged — this keeps every
    existing caller working exactly as before.

    `tone_hint` is optional — pass the result of
    emotion_detector.get_tone_hint(...) to nudge this one reply's
    tone based on the user's detected mood (or fully override to a
    caring, non-persona tone if a crisis signal was detected). Leave
    blank for normal replies; nothing changes if you never pass it.
    """
    name = nickname or DEFAULT_NICKNAME
    if persona_override:
        # 🌸 Custom/preset personality replaces the traits, but identity
        # facts (name/creator/repo) are the same regardless of vibe —
        # keep those so "who made you" / "source code?" still work.
        base = (
            f"ur name is {name}, a Discord bot. {persona_override} "
            f"you were made by <@{CREATOR_ID}> using python and discord.py, if "
            "someone asks who made you or what you're built with, just answer "
            f"that consistently. if they ask for your source code, drop this "
            f"link: {REPO_URL}"
        )
    else:
        base = PERSONALITY_TEMPLATE.format(nickname=name)
    if tone_hint:
        return f"{base}\n\n🌸 mood check for THIS reply only: {tone_hint}"
    return base


async def load_personality_with_override(
    bot,
    guild_id: int | None,
    channel_id: int | None,
    nickname: str | None,
    tone_hint: str = "",
) -> str:
    """
    🌸 Resolves the active AI personality for a reply, checking for a
    per-server (guild_id) or per-DM-channel (channel_id) override
    BEFORE falling back to the default nickname-based template. This
    is the function build_system_prompt in engine/prompt.py should
    call instead of get_personality_for_nickname directly, so
    /persona-set overrides actually take effect.

    Lookup order:
      1. guild_id given (we're in a server) → check that guild's
         ai_persona override via the ServerPersona cog.
      2. guild_id is None (we're in a DM) → check THIS DM channel's
         own dm_ai_persona override, scoped by channel_id — mirrors
         the guild lookup exactly, just channel_id instead of
         guild_id as the scope key. Each DM channel gets its own
         override, same as each guild does.
      3. No override found (or the cog isn't loaded for some reason)
         → fall back to get_personality_for_nickname with no override,
         i.e. byte-for-byte the old behavior.
    """
    persona_override = None
    try:
        cog = bot.get_cog("ServerPersona") if bot else None
        if cog is not None:
            if guild_id is not None:
                persona_override = await cog.get_guild_ai_persona(guild_id)
            elif channel_id is not None:
                persona_override = await cog.get_dm_ai_persona(channel_id)
    except Exception as e:
        print(f"⚠️ Persona override lookup failed (guild={guild_id}, channel={channel_id}): {e}")
        persona_override = None

    return get_personality_for_nickname(nickname, tone_hint, persona_override)


async def load_personality(bot, guild_id: int, tone_hint: str = "") -> str:
    """
    🌸 Fetch the bot's CURRENT per-guild nickname for guild_id and build
    personality instructions from it. Reads guild.me.nick fresh every
    call (nothing cached), so changing the nickname via
    /server-persona-set takes effect on the very next AI reply.

    `tone_hint` — see get_personality_for_nickname; pass through the
    per-message emotion read here so mood adjustments apply on top
    of whatever nickname/persona this guild currently has set.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild and guild.me:
            return get_personality_for_nickname(guild.me.nick, tone_hint)
    except Exception as e:
        print(f"⚠️ Error loading personality for guild {guild_id}: {e}")

    return get_personality_for_nickname(None, tone_hint)

