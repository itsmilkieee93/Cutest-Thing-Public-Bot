"""🌸 Memory-window module — extracted from groq_ai.py's monolithic
get_ai_response. Structure-only split: same per-guild/per-DM-channel
scoping, same last-200-turns cap, same shared-across-models storage,
same random window sizing, same reply-anchor slicing, same interceptor
fallback-context turn.

load_history_and_window(...) returns everything the completion loop
and the memory-save step need:
  (history, recent_history, guild_id, dm_channel_id)

`history` is the FULL loaded list (up to 200 turns) — callers append
the new user/assistant turns to THIS and save it back, exactly as the
original inline code did. `recent_history` is the trimmed window
actually sent to the model this call.
"""
import random

from resources import shared


async def load_history_and_window(
    *,
    user_id: int,
    guild,
    channel,
    is_compound_call: bool,
    reply_to_message_id,
    reply_to_message_text,
):
    """
    🌸 user_id here is always the Discord snowflake ID of whoever
    actually pinged/replied to the bot (passed straight through from
    message.author.id in on_message / handle_mention_reaction), so
    memory always follows the right person even if their Discord
    username changes later. Memory is now scoped PER-GUILD as well
    as per-user — guild.id (or 0 for DMs) keeps what someone says
    in one server from leaking into another server's context, while
    still being shared across every model in MODEL_POOL within that
    guild (doesn't matter which model answered last turn).

    🌸 DMs (guild=None) now get their own per-channel memory bucket
    — groq/memory/dm/{channel_id}/memory.db — instead of sharing
    one flat groq/memory/0/ bucket. channel here is message.channel
    passed through from handle_mention_reaction; for a DM that's
    the DMChannel object, and .id is stable per user. Guild
    messages don't need this — channel_id is ignored whenever
    guild_id is truthy (see shared._groq_bucket_key).
    """
    guild_id = guild.id if guild else 0
    dm_channel_id = channel.id if (channel and not guild) else None
    history = await shared.load_groq_memory(user_id, guild_id, dm_channel_id)

    # 🌸 SLICING: only ship a random-sized recent window (from THIS
    # user_id's shared history) to Groq to keep input tokens small and
    # vary the model's context a bit turn to turn. `history` itself
    # stays FULL (up to 200 turns) — it's what gets appended to and
    # saved back to memory.db below, so nothing is lost from long-term
    # memory. `recent_history` is just the window actually sent to
    # the API this call: random.randint(2, 8) * 2 gives an even count
    # of 4, 6, 8, 10, 12, 14, or 16 messages.
    #
    # 🌸 ANCHOR POINT: normally the window is just the newest tail of
    # `history` (someone continuing the live conversation). But if
    # `reply_to_message_id` is set — meaning the user swiped up and
    # replied to an OLD bot message instead of the most recent one —
    # we instead anchor the window to END right after that old
    # assistant turn, so the random slice pulls the messages
    # surrounding THAT point in time rather than today's tail. This
    # only works for turns saved after this feature shipped (they
    # carry a "message_id" key); older turns without one are just
    # skipped when searching for the anchor, so nothing crashes on
    # legacy rows — it just falls back to normal tail slicing.
    recent_turn_count = random.randint(2, 8) * 2
    anchor_end = len(history)

    # 🌸 COMPOUND TOKEN BUDGET: groq/compound and groq/compound-mini
    # burn noticeably more tokens per call than a plain chat model —
    # the search tool's query + Tavily's returned snippets all get
    # fed back into the model as EXTRA input tokens on top of
    # whatever history we send, and Groq's free/on-demand TPM ceiling
    # can be as low as ~6-12k tokens/minute. A history window sized
    # fine for gpt-oss/llama can tip a compound call over that limit
    # and 413 ("Request Entity Too Large"). Cap the window hard for
    # compound so there's headroom left for the tool round-trip.
    # On-demand free tier is especially tight, so clamp to just 2 turns
    # (1 exchange) instead of 4 to maximize margin.
    if is_compound_call:
        recent_turn_count = min(recent_turn_count, 2)

    if reply_to_message_id is not None:
        for idx in range(len(history) - 1, -1, -1):
            turn = history[idx]
            if (
                turn.get("role") == "assistant"
                and turn.get("message_id") == reply_to_message_id
            ):
                # 🌸 +1 so the slice INCLUDES this assistant turn
                # itself (and its matching user turn right before
                # it), not just everything strictly older than it.
                anchor_end = idx + 1
                break
            # 🌸 If no match is found (message too old to still be in the
            # last 200 saved turns, or it predates this feature), anchor_end
            # just stays len(history) — same behavior as before, no crash.

    window_start = max(0, anchor_end - recent_turn_count)

    # 🌸 HIGHLIGHT FLAG: when there's a real anchor (reply_to_message_id
    # matched something in history), tag the anchored user+assistant
    # pair so Groq can tell "this exact exchange is what they're
    # replying to" apart from the rest of the window, which is just
    # loose surrounding context. Without this, a window that happens
    # to sandwich the anchor next to something more recent/salient
    # (e.g. a GitHub link exchange saved right after it) can pull
    # Groq's attention toward the WRONG turn — it has no way to know
    # which pair in the flat list is the one actually being replied
    # to. anchor_start marks where the flagged pair begins (usually
    # anchor_end - 2, i.e. the user turn immediately followed by the
    # assistant turn that matched reply_to_message_id).
    is_anchored = reply_to_message_id is not None and anchor_end != len(history)
    anchor_start = anchor_end - 2 if is_anchored else None

    recent_history = []
    for i, t in enumerate(history[window_start:anchor_end], start=window_start):
        content = t["content"]
        if is_anchored and i == anchor_start:
            content = f"[REPLYING TO THIS ⬇️] {content}"
        elif is_anchored and i == anchor_start + 1:
            content = f"[THIS IS THE MESSAGE BEING REPLIED TO] {content}"
        recent_history.append({"role": t["role"], "content": content})

    # 🌸 FALLBACK CONTEXT: fires when this WAS a genuine reply-to-bot
    # (reply_to_message_id is set) but the anchor search above found
    # NOTHING in Groq's memory — meaning the replied-to message was
    # never saved as a history turn at all. This is true for every
    # interceptor reply (avatar/owner/verification/server-info/media/
    # etc. in bot_service.handle_mention_reaction) since those
    # short-circuit and reply BEFORE get_ai_response is ever called.
    # Without this, "summarize it" replying to a server-info embed
    # would fall back to plain tail slicing and Groq would answer
    # about whatever's in today's tail instead (e.g. its own GitHub
    # self-intro) — see the screenshots that prompted this fix.
    # Injected as its own flagged pseudo-turn at the END of
    # recent_history (appended, not spliced into `history` — this
    # never gets saved back to memory.db, it's request-only) so it
    # reads the same way to Groq as a real anchored exchange above.
    if (
        reply_to_message_id is not None
        and not is_anchored
        and reply_to_message_text
    ):
        recent_history.append({
            "role": "assistant",
            "content": f"[THIS IS THE MESSAGE BEING REPLIED TO] {reply_to_message_text}",
        })

    return history, recent_history, guild_id, dm_channel_id
