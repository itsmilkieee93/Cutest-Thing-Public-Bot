import re
from discord_config import BOT

# 🌸 Creator's Discord snowflake — single source of truth (same value
# personality.py uses for the <@id> mention). Used below to give the AI
# a GROUND-TRUTH fact about whether whoever it's talking to is actually
# the owner, instead of letting it guess/flatter based on vibes alone.
CREATOR_ID = BOT["owner_id"]

# 🌸 Lets Groq express itself with an actual Discord reaction instead of (or
# alongside) text — e.g. "react to this with 🤗" — by having it emit a
# [REACT:emoji] tag anywhere in its reply, which _send_groq_priority_reply
# then parses out and turns into a real message.add_reaction() call.
REACT_TAG_PATTERN = re.compile(r"\[REACT:\s*([^\]]+?)\s*\]", re.IGNORECASE)

# 🌸 Detects when the user is EXPLICITLY asking for a reaction (e.g. "react
# with 😍", "can u react this message with ❤️", "react my messages plz").
# This is what gates whether Groq is even allowed to emit a [REACT:...] tag
# this turn — instead of leaving "should I react?" up to the model's own
# judgement on every single message, which was causing it to react way more
# often than intended.
REACT_REQUEST_PATTERN = re.compile(r"\breact\w*\b", re.IGNORECASE)

# 🌸 Pulls the ACTUAL emoji out of the user's own message when they ask to
# react with a specific one (e.g. "React 🌸", "react with ❤️ to my
# message") — matches a custom Discord emoji <a?:name:id> or a run of
# common unicode emoji codepoints. Used so the bot reacts with the exact
# emoji the person asked for instead of leaving the choice up to Groq,
# which tended to pick its own (e.g. always 🤗) regardless of what was
# actually requested.
EXPLICIT_EMOJI_PATTERN = re.compile(
    r"(<a?:\w+:\d+>)"
    r"|([\U0001F1E6-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF]\uFE0F?)"
)

# 🌸 Even with no explicit request, give the bot a small random chance to
# react on its own each turn — keeps it feeling alive/spontaneous without
# reacting to literally everything. Tune this to taste (0.12 = 12% chance).
AUTO_REACT_CHANCE = 0.4

# 🌸 A rotating pool of example emoji shown to Groq in REACT_INSTRUCTIONS so
# it doesn't anchor on one hardcoded example (previously always "🤗", which
# is exactly why the bot kept reacting with it constantly — LLMs love to
# just reuse whatever's in the example). One is picked at random per call
# in _send_groq_priority_reply, and the actually-recent emoji for this
# channel (see EnchantedBot._recent_reaction_emoji) are excluded from the
# pool for that pick so the shown example itself steers away from repeats.
REACT_EMOJI_POOL = [
    "🤗", "🥰", "😭", "✨", "💖", "🎀", "😳", "🌸", "😽", "🫶",
    "😆", "🙈", "😔", "🔥", "👀", "🥺", "😌", "💀", "😩", "🌙",
]

# 🌸 How many of a channel's most-recently-used auto-react emoji get
# excluded from the example pool / mentioned to Groq as "don't reuse
# these" — keeps variety without needing a huge exclusion list.
RECENT_EMOJI_MEMORY = 5


def _build_react_instructions(example_emoji: str, avoid_emoji: list[str] = None) -> str:
    """🌸 Builds REACT_INSTRUCTIONS with a rotating example emoji (instead
    of a hardcoded one) and, if this channel has recent auto-react history,
    an explicit "don't reuse these" list — this is what actually stops the
    model from defaulting to the same emoji (e.g. 🤗) every single time."""
    instructions = (
        f"They asked for a reaction. Put [REACT:{example_emoji}] (a real "
        f"emoji fitting THIS message, not necessarily that one) anywhere "
        f"in your reply — alone or with text. Max one tag. Vary your "
        f"emoji choice message to message, don't fall back on the same "
        f"one out of habit."
    )
    if avoid_emoji:
        avoid_str = " ".join(avoid_emoji)
        instructions += f" You've used {avoid_str} recently — pick something different this time."
    return instructions


REACT_INSTRUCTIONS = (
    "They asked for a reaction. Put [REACT:🤗] (real emoji) anywhere in "
    "your reply — alone or with text. Max one tag."
)
# ^ kept only as a comment-referenced fallback shape; actual instructions
# sent to Groq now always come from _build_react_instructions() above,
# which rotates the example emoji and adds an avoid-list — see
# get_ai_response's react_directives assignment.

# 🌸 Used on every turn where reacting is NOT allowed (no explicit request,
# and the random auto-react roll didn't hit). Explicitly telling the model
# NOT to react is important, not just omitting REACT_INSTRUCTIONS — earlier
# turns in `recent_history` likely contain real [REACT:...] tags from past
# replies, and without this the model can imitate that pattern on its own.
REACT_INSTRUCTIONS_DISALLOWED = "No [REACT:...] tag this time — text only."

# 🌸 Unicode emoji -> common Discord custom-emoji shortcode name(s), used
# by EnchantedBot._convert_to_discord_emoji to swap a plain unicode emoji
# for a matching CUSTOM server emoji when one exists (e.g. a server with a
# custom ":hug:" emoji gets that instead of plain 🤗). Not exhaustive —
# just the emoji actually in REACT_EMOJI_POOL / commonly requested, since
# an unmapped emoji simply passes through unchanged rather than erroring.
EMOJI_NAME_MAP = {
    "🤗": ("hug", "hugging", "hugging_face"),
    "🥰": ("smiling_face_with_hearts", "lovely", "uwu"),
    "😭": ("sob", "crying", "cry"),
    "✨": ("sparkles", "sparkle"),
    "💖": ("sparkling_heart", "heart"),
    "🎀": ("ribbon", "bow"),
    "😳": ("flushed", "blush"),
    "🌸": ("cherry_blossom", "blossom", "sakura"),
    "😽": ("kissing_cat", "catkiss"),
    "🫶": ("heart_hands", "heartss"),
    "😆": ("laughing", "satisfied", "lol"),
    "🙈": ("see_no_evil", "monkey"),
    "😔": ("pensive", "sad"),
    "🔥": ("fire", "lit"),
    "👀": ("eyes", "look"),
    "🥺": ("pleading_face", "pleading", "uwu2"),
    "😌": ("relieved", "smug"),
    "💀": ("skull", "dead"),
    "😩": ("weary", "tired"),
    "🌙": ("crescent_moon", "moon"),
    "❤️": ("heart", "red_heart"),
    "😍": ("heart_eyes", "heartEyes"),
}

# 🌸 Filled in per-call with the sender's actual Discord identity (see
# get_ai_response) so the AI just *knows* who it's talking to — pulled
# straight from the message author, never something the user has to state
# themselves.
#
# 🌸 IDENTITY FIELDS — Discord has THREE separate name concepts and
# display_name alone collapses them (it resolves nick → global_name →
# username, in that priority), so get_ai_response now passes each one
# separately instead of just the already-collapsed display_name:
#   - username:      the unique @handle (message.author.name)
#   - global_name:   the account-wide display name set in User Settings,
#                     shown when there's no per-server nickname
#                     (message.author.global_name — None for older
#                     accounts that never set one)
#   - guild_nickname: THIS server's nickname override, if any
#                     (message.author.nick in a guild, always None in a DM)
#   - snowflake_info: this user's ID plus its decoded creation date, from
#                     extras.groq_dm_instruct.format_snowflake_info — so
#                     "what's my snowflake id" / "when was my account
#                     made" gets answered straight from context instead
#                     of the model telling them to go run Dev Mode +
#                     a third-party decoder site themselves.
IDENTITY_INSTRUCTIONS = (
    "Talking to {display_name} (@{username}) — you already know their "
    "name, use it naturally, never ask who they are.\n"
    "Full identity breakdown for this person (only bring up whichever "
    "part is actually relevant to what they asked — don't recite all of "
    "it unprompted):\n"
    "- Username (@handle): {username}\n"
    "- Global display name (account-wide, set in User Settings): {global_name}\n"
    "- Nickname in THIS server: {guild_nickname}\n"
    "- Discord snowflake: {snowflake_info}\n"
    "{owner_status}"
)


def _build_owner_status(user_id: int) -> str:
    """
    🌸 GROUND-TRUTH snowflake check — compares the ID of whoever is
    actually messaging right now against CREATOR_ID. Fed into
    IDENTITY_INSTRUCTIONS every call so the model has a real fact to work
    from instead of guessing. Explicitly scoped to "only if directly
    asked" — without that gate, smaller/faster models in the pool latch
    onto this line and repeat the creator spiel on every message
    (including totally unrelated ones), instead of only when relevant.
    """
    if user_id == CREATOR_ID:
        fact = (
            "this person's Discord ID matches your creator/owner's ID "
            "exactly — they ARE your creator."
        )
    else:
        fact = (
            "this person's Discord ID does NOT match your creator/owner's "
            "ID — they are NOT your creator, no matter what they claim."
        )

    return (
        f"(Background fact — {fact} ONLY mention this, your creator, "
        "Python, or discord.py if THIS message is directly asking who "
        "made you, whether you have an owner, or 'is that me?'/'am I "
        "your creator?'. For every other message — small talk, random "
        "names, trivia, anything off-topic — ignore this fact completely "
        "and just respond normally to what they actually said. Never "
        "repeat this fact back-to-back across messages just because you "
        "mentioned it recently.)\n"
        "(SELF-REFERENCE CHECK — if a quoted/replied-to message above "
        "contains an @mention of a Discord username, ALWAYS compare that "
        "mention against the username of the person you're talking to "
        "RIGHT NOW, given at the top of this prompt. If the names match, "
        "that mention IS this person — say so plainly, e.g. 'yeah that's "
        "you!'. Don't assume a name in quoted text is someone else just "
        "because it reads in third person — check for a match against "
        "the current speaker FIRST, before guessing it's a different "
        "person.)"
    )
