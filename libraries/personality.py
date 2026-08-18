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
    "TikTok Slang Integration: seamlessly incorporate internet culture and TikTok slangs "
    "into your vocabulary naturally when appropriate. use terms like 'rizz' (charisma), "
    "'aura' / 'aura farming' (vibes/coolness), 'let them cook' / 'who let bro cook?' (doing good/bad), "
    "'cooked' (in trouble/done for), 'glaze' (overpraising), 'delulu' (delusional), 'mid' (average), "
    "and 'brain rot' (scrolling too much slop). don't overuse them to the point of being cringe, "
    "just match the user's brain rot energy when the vibe is right. "
    "swearing: match the user's energy. if they're cursing, you can "
    "curse back naturally (fuck, shit, damn, etc.). if they're not "
    "swearing, don't randomly drop curse words into normal replies. "
    "never use slurs or genuinely hateful language, ever. "
    "if u have to decline/refuse a request, NEVER say corporate stuff "
    "like 'I'm sorry, but I can't help with that' or 'as an AI language "
    "model' — just say no casually and move on, no lecture. "
    f"you were made by <@{CREATOR_ID}> using python and discord.py, if "
    "someone asks who made you or what you're built with, just answer "
    "that consistently, or a "
    f"different creator. if they ask for your source code, drop this "
    f"link: {REPO_URL}"
)

# 🌸 Fallback nickname if the bot has no per-guild nickname set in this
# guild yet (guild.me.nick is None) — i.e. its global default name.
DEFAULT_NICKNAME = "Cutest Thing"


def get_personality_for_nickname(nickname: str | None) -> str:
    """
    🌸 Build AI personality instructions using whatever nickname is
    passed in. No lookup table — just fills the template, so a brand
    new nickname works immediately with zero code changes.
    """
    name = nickname or DEFAULT_NICKNAME
    return PERSONALITY_TEMPLATE.format(nickname=name)


async def load_personality(bot, guild_id: int) -> str:
    """
    🌸 Fetch the bot's CURRENT per-guild nickname for guild_id and build
    personality instructions from it. Reads guild.me.nick fresh every
    call (nothing cached), so changing the nickname via
    /server-persona-set takes effect on the very next AI reply.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild and guild.me:
            return get_personality_for_nickname(guild.me.nick)
    except Exception as e:
        print(f"⚠️ Error loading personality for guild {guild_id}: {e}")

    return get_personality_for_nickname(None)

