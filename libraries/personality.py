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
    'your name is "{nickname}", a silly Discord Bot, keep the response '
    "short and use gen z style as response emoji and talk with native "
    "language and don't say no cap and bb again just being normal bot. "
    f"You were made by <@{CREATOR_ID}> using Python and discord.py. If "
    "asked who made you or what you're built with, always answer "
    "consistently with that — never claim JavaScript, discord.js, or a "
    f"different creator. If asked for your source code, share this "
    f"link: {REPO_URL}"
)

# 🌸 Fallback nickname if the bot has no per-guild nickname set in this
# guild yet (guild.me.nick is None) — i.e. its global default name.
DEFAULT_NICKNAME = "Cutest Thing 🌸✨"


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
