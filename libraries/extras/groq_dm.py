"""🌸 extras/groq_dm.py

Tiny helper module that owns the "should the bot treat this DM as a
mention?" decision, split out of bot_service.py's on_message so the
DM-trigger logic has a home separate from the guild mention/reply gate
in groq_service.py (GroqMentionService).

There's no AI/handling logic here — handle_mention_reaction in
groq_service.py already works fine for DMs (guild is None there, and
the server-query dispatch block is gated behind `if guild:`, so it
falls straight through to general chat). This module is purely the
gate: "is this message a DM the bot should respond to at all."

Usage in bot_service.py:

    from extras.groq_dm import is_dm_trigger

    ...
    if is_dm_trigger(message, self.user) or is_mentioned or await self.groq_mentions.is_reply_to_bot(message):
        await self.groq_mentions.handle_mention_reaction(message)
"""

import discord


def is_dm_trigger(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    """🌸 True if `message` is a DM the bot should respond to.

    Kept as its own function (rather than an inline isinstance check)
    so the DM-eligibility rule has one place to grow — e.g. later
    excluding group DMs, ignoring empty/attachment-only messages, or
    adding a DM-specific opt-out — without touching on_message itself.

    NOTE: message.author.bot filtering already happens earlier in
    on_message before this is ever called, so we don't re-check it
    here — this function assumes it's only called for human authors.
    """
    if not isinstance(message.channel, discord.DMChannel):
        return False

    # 🌸 Guard against the bot's own messages / other bots' DMs ever
    # looping back through this — belt-and-suspenders on top of the
    # author.bot check in on_message, since bot_user is cheap to compare.
    if message.author.id == bot_user.id:
        return False

    return True
