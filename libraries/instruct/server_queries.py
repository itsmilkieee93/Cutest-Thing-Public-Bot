import re
import random
import asyncio
import discord
from datetime import datetime

from instruct.models_search import CLASSIFIER_MODEL

# 🌸 Discord's snowflake epoch (2015-01-01T00:00:00.000Z) — used to convert
# between a plain timestamp and a Discord message ID for time-window roulette.
DISCORD_EPOCH_MS = 1420070400000

# 🌸 ROLE QUERY DETECTION — when user asks "who has X role" or "which members have Y",
# bypass Groq entirely and query the guild cache directly (instant + token-free!)
#
# Two word orders are supported, tried in order inside handle_role_query:
#   A) "role LAST"  — who/which has/have/got (the/a) <ROLE> role/position
#      e.g. "who has the admin role", "which members have mod position"
#   B) "role FIRST" — who has/got (the) role of/called/named <ROLE>
#      e.g. "who has role of P", "who has the role called Admin"
# _ROLE_VERB widened to also catch "who's got"/"got" so casual phrasings
# aren't missed just for skipping the more formal "have".
_ROLE_VERB = r"(?:has|have|had|have\s*been|'ve\s*got|got)"

# 🌸 Optional noun after who/which ("who ELSE has...", "which MEMBERS
# have...", "which PEOPLE have...") so those don't fall through just
# because a word sits between the question-word and the verb.
_WHO_WHICH_SUBJECT = r"(?:who|which)(?:\s+members|\s+people|\s+else)?"

ROLE_QUERY_PATTERN = re.compile(
    rf"\b(who|which)(?:\s+members|\s+people|\s+else)?\s+{_ROLE_VERB}\s+(?:the\s+|a\s+)?([a-zA-Z0-9\s\-]+?)\s+(role|position)\b",
    re.IGNORECASE
)

# 🌸 "role FIRST" word order — role name comes AFTER "role of/called/named"
# instead of before "role"/"position". Only 1 capture group (the role
# name itself) — handle_role_query checks which pattern matched before
# picking the right group index, since ROLE_QUERY_PATTERN has 4 groups
# but this one only has 1.
ROLE_QUERY_PATTERN_ROLE_FIRST = re.compile(
    rf"\b{_WHO_WHICH_SUBJECT}\s+{_ROLE_VERB}\s+(?:the\s+|a\s+)?role\s+(?:of|called|named)\s+([a-zA-Z0-9\s\-]+?)(?:[?.!]|$)",
    re.IGNORECASE
)

# 🌸 "DOES USER HAVE ROLE X" DETECTION — a totally different query shape
# from the two above: those ask "who HAS role X" (role → member list),
# this asks "does MEMBER have role X" (member → yes/no + role name).
# Needs a mentioned/named user AND a role name, so it's handled by a
# separate function (handle_user_role_query) that resolves the target
# member via message.mentions rather than shared.get_members_with_role.
#
# Three role-name shapes, tried as alternatives (in this order so the
# more specific "role of/called/named X" wins over the bare "role X"
# reading when both could apply):
#   1. "...role of/called/named X"  (group 2)  — "has role called VIP"
#   2. "...role X" (bare, no connector) (group 3) — "has role p"
#   3. "...X role" (role LAST)      (group 4)  — "has the admin role"
# Using alternation (rather than one greedy name group with an optional
# trailing "role") avoids the earlier bug where a literal trailing
# "role" word got absorbed INTO the captured name instead of being
# recognized as the word "role" itself.
USER_ROLE_QUERY_PATTERN = re.compile(
    rf"\bdoes\s+(.+?)\s+{_ROLE_VERB}\s+(?:the\s+|a\s+)?"
    rf"(?:role\s+(?:of|called|named)\s+([a-zA-Z0-9\s\-]+?)"
    rf"|role\s+([a-zA-Z0-9\s\-]+?)"
    rf"|([a-zA-Z0-9\s\-]+?)\s+role)"
    rf"\s*\??$",
    re.IGNORECASE
)

# 🌸 DB-INTERCEPTOR REPHRASE STRATEGY — same "strategy" coin-flip idea as
# groq_service.py's random.choice(["ai", "regex"]) for classification
# order, applied here to OUTPUT instead: a raw DB-interceptor string
# (e.g. "**P** (13): name1, name2, ... 🌸") is correct and instant, but
# reads like a flat data dump every single time. Sending it through Groq
# on every call would restore full per-message cost/latency, defeating
# the whole point of a zero-token interceptor. Instead, roll a random
# per-query chance of rephrasing — most replies stay instant/free, but
# a fraction get a natural, varied restatement, so the bot doesn't sound
# robotically identical every time someone asks the same kind of
# question.
REPHRASE_CHANCE = 0.35  # 🌸 tune this to trade cost vs. variety


async def maybe_rephrase_db_result(raw_text: str, user_message: str, groq_client, username: str) -> str:
    """
    🌸 Optionally sends a raw DB-interceptor result through a cheap Groq
    call to restate it more naturally, for reply variety. Rolls
    REPHRASE_CHANCE odds — most calls return raw_text completely
    unchanged (same instant/zero-token path as before). Only the WORDING
    is ever touched by the model; the actual data (names, counts) is
    fixed in the prompt as ground truth it must preserve, not regenerate
    from scratch, so this can't introduce a hallucinated member list the
    way letting the raw question fall through to general chat could.

    groq_client: the SYNC Groq client (e.g. self.bot.groq.client), passed
    in the same way handle_math_request already receives it — this
    module has no client of its own.

    Fails open to raw_text unchanged on ANY problem (no client, rate
    limit, empty reply, network blip) — a skipped rephrase is never
    worse than the plain result it started from.
    """
    if not groq_client or not raw_text:
        return raw_text

    if random.random() > REPHRASE_CHANCE:
        return raw_text

    loop = asyncio.get_running_loop()

    def _call():
        return groq_client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You restate a bot's database-lookup result in a more "
                        "natural, casual, kawaii-flavored way for Discord. The "
                        "FACTS below (names, counts, role names) are ground "
                        "truth — you may reorder/rephrase the SENTENCE around "
                        "them, but you must NOT add, remove, or invent any "
                        "name or number that isn't already there. Keep any "
                        "emoji. Keep it short — one or two sentences. Output "
                        "ONLY the restated reply, nothing else."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{username} asked: {user_message[:200]!r}\n"
                        f"Raw result to restate: {raw_text}"
                    ),
                },
            ],
            temperature=0.8,
            reasoning_effort="low",
            max_tokens=200,
        )

    try:
        response = await loop.run_in_executor(None, _call)
        rephrased = (response.choices[0].message.content or "").strip()
        return rephrased if rephrased else raw_text
    except Exception as e:
        print(f"⚠️ DB-result rephrase failed (using raw result): {e}")
        return raw_text


async def handle_role_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False, role_name_hint: str | None = None, groq_client=None) -> str | None:
    """
    🌸 REGEX/AI ROLE QUERY HANDLER — intercepts "who has the X role",
    "which members have Y" before they hit Groq, queries roles.db
    directly via shared.get_members_with_role.

    skip_pattern_check: trust an AI classifier's "role_query" label
    instead of re-gating on ROLE_QUERY_PATTERN — same contract as
    every other handler in this file's skip_pattern_check param.
    Unlike those handlers though, this one's regex isn't JUST a gate —
    it also EXTRACTS the role name via a capture group, so skipping the
    regex alone would leave nothing to look up. That's what
    role_name_hint is for: when skip_pattern_check=True, pass the role
    name the classifier already picked out of the message (e.g. "green
    name" from "who's got the green name role") so a phrasing outside
    ROLE_QUERY_PATTERN's rigid "who/which has/have the X role/position"
    shape can still resolve. If skip_pattern_check=True and
    role_name_hint is empty/None, this still falls back to trying the
    regex on message.content before giving up (belt-and-suspenders,
    matches the "fail open to the next interceptor" spirit of the rest
    of this file) rather than raising or guessing.

    🐛 FIXED: previously read match.group(5), but ROLE_QUERY_PATTERN
    only has 4 capture groups (role name is group 3) — every real call
    hit an IndexError, was swallowed by the except below, and silently
    returned None every single time. This handler could never actually
    succeed until this fix.

    🌸 EXTENDED: now also tries ROLE_QUERY_PATTERN_ROLE_FIRST (role name
    AFTER "role of/called/named", e.g. "who has role of P") when the
    role-LAST pattern doesn't match — previously that word order fell
    all the way through to general chat, which is exactly what silently
    failed in the reported "who has role of P" case.
    """
    role_name_raw = (role_name_hint or "").strip()

    if not role_name_raw:
        match = ROLE_QUERY_PATTERN.search(message.content)
        if match:
            role_name_raw = match.group(3).strip()
        else:
            # 🌸 Try the "role FIRST" word order before giving up.
            match = ROLE_QUERY_PATTERN_ROLE_FIRST.search(message.content)
            if match:
                role_name_raw = match.group(1).strip()

        if not role_name_raw:
            # 🌸 Neither word order matched. Fail open so the caller
            # falls through to the next interceptor/general chat, same
            # as every other empty-result path here.
            return None

    try:
        # Try exact match first
        members = await shared.get_members_with_role(guild_id, role_name_raw)
        
        # If no exact match, try partial match (contains)
        if not members:
            all_roles = await shared.list_all_roles(guild_id)
            matching_roles = [r for r in all_roles if role_name_raw.lower() in r['name'].lower()]
            
            if matching_roles and len(matching_roles) == 1:
                # Only one partial match found, use it
                members = await shared.get_members_with_role(guild_id, matching_roles[0]['name'])
        
        if not members:
            return f"❌ No role found called '{role_name_raw}' or nobody has it yet."
        
        # 🌸 Show up to 100 names instead of the old hard cap of 5 — a
        # role like Contributor (16 members) used to get truncated to
        # "name1, name2, name3, name4, name5 +11 more", which answers
        # "how many" but not "who". 100 covers virtually every role on
        # this server without risking Discord's ~2000-char message
        # limit (even with several long display names, 100 comma-
        # separated entries stays well under that in practice — if a
        # role ever legitimately has 100+ members, "+N more" still
        # kicks in below rather than silently dropping the rest).
        MAX_NAMES_SHOWN = 100
        member_names = [m.get("display_name") or m.get("username") for m in members[:MAX_NAMES_SHOWN]]
        more_count = len(members) - MAX_NAMES_SHOWN
        more_text = f" +{more_count} more" if more_count > 0 else ""
        
        raw_result = f"**{role_name_raw}** ({len(members)}): {', '.join(member_names)}{more_text} 🌸"

        # 🌸 Strategy roll — see maybe_rephrase_db_result docstring above.
        # Most calls return raw_result unchanged (same instant/free path
        # as before); a random fraction get restated more naturally.
        return await maybe_rephrase_db_result(raw_result, message.content, groq_client, message.author.name)
    except Exception as e:
        print(f"⚠️ Role query error: {e}")
        return None


async def handle_user_role_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False, groq_client=None) -> str | None:
    """
    🌸 "DOES USER HAVE ROLE X" HANDLER — the mirror-image of
    handle_role_query above. That one goes role → member list ("who has
    the P role"); this one goes member → yes/no ("does its.cuteee_ have
    role P"). They're different lookups (get_members_with_role vs.
    get_member_roles) so they get separate handlers rather than trying
    to cram both directions through one regex/function — that's exactly
    what silently failed in the reported "does its.cuteee_ has role p"
    case (ROLE_QUERY_PATTERN's "who/which" start never matches "does").

    Target member resolution priority:
      1. message.mentions (an actual @mention in the message) — most
         reliable, no name-collision risk.
      2. USER_ROLE_QUERY_PATTERN's captured name, matched against the
         guild's member cache by display_name/username (case-insensitive
         exact match first, then unique substring match) — covers the
         plain-text case from the screenshot ("does its.cuteee_ has role p",
         no @ mention).

    Fails open (returns None) on: no regex match, no resolvable member,
    ambiguous name match (2+ members), or any DB error — same
    "never worse than falling through" contract as handle_role_query.
    """
    target_id: int | None = None
    target_display: str | None = None
    role_name_raw = ""

    # 🌸 Prefer an actual Discord @mention when present — zero ambiguity.
    if message.mentions:
        target_id = message.mentions[0].id
        target_display = message.mentions[0].display_name or message.mentions[0].name

    match = USER_ROLE_QUERY_PATTERN.search(message.content)
    if not match:
        if skip_pattern_check:
            return None
        return None

    name_hint = match.group(1).strip().strip("@")
    role_name_raw = (match.group(2) or match.group(3) or match.group(4) or "").strip()

    if not role_name_raw:
        return None

    if target_id is None:
        # 🌸 No @mention — resolve the plain-text name against the
        # guild's cached members via shared.find_member_by_name, which
        # already does exact-then-substring matching and returns []/1/2+
        # for not-found/resolved/ambiguous respectively.
        try:
            matches = await shared.find_member_by_name(guild_id, name_hint)
        except Exception as e:
            print(f"⚠️ User-role query member lookup error: {e}")
            return None

        if len(matches) == 1:
            target_id = matches[0]["id"]
            target_display = matches[0].get("display_name") or matches[0].get("username")

    if target_id is None:
        # 🌸 Couldn't confidently resolve who "its.cuteee_" etc. refers
        # to (not found, or ambiguous) — fail open to the next
        # interceptor/general chat rather than guessing.
        return None

    try:
        member_roles = await shared.get_member_roles(guild_id, target_id)

        role_names_lower = [r["name"].lower() for r in member_roles]
        role_name_lower = role_name_raw.lower()

        has_role = role_name_lower in role_names_lower
        # 🌸 Fall back to partial match (e.g. asked "mod" but role is
        # "Moderator") only when there's exactly one plausible candidate
        # among the member's OWN roles — avoids false positives/negatives
        # from a loose substring match against an unrelated role name.
        if not has_role:
            partial_matches = [r for r in member_roles if role_name_lower in r["name"].lower()]
            if len(partial_matches) == 1:
                has_role = True
                role_name_raw = partial_matches[0]["name"]

        display = target_display or str(target_id)
        if has_role:
            raw_result = f"✅ Yep, **{display}** has the **{role_name_raw}** role! 🌸"
        else:
            raw_result = f"❌ Nope, **{display}** doesn't have the **{role_name_raw}** role."

        return await maybe_rephrase_db_result(raw_result, message.content, groq_client, message.author.name)
    except Exception as e:
        print(f"⚠️ User-role query error: {e}")
        return None


# 🌸 "IS USER ON THIS SERVER" DETECTION — a THIRD query shape, distinct
# from both role queries above: those assume the person is already here
# and ask about a ROLE. This one asks pure EXISTENCE/MEMBERSHIP — "is
# moonaesthetic0435 on this server", "does @bob exist in this server",
# "is there a user named X". Three phrasings, tried in order:
#   A) "is X on/in/a member of this server"
#   B) "does X (exist) on/in this server"      — covers the awkward-
#      grammar phrasing seen in the field ("does he on this server")
#   C) "is there a user/member/person named/called X"
MEMBER_LOOKUP_PATTERN_A = re.compile(
    r"\bis\s+(@?[\w.\-]+?)\s+(?:on|in|a\s+member\s+of|part\s+of)\s+(?:this\s+|the\s+)?(?:server|guild|discord)\b",
    re.IGNORECASE,
)
MEMBER_LOOKUP_PATTERN_B = re.compile(
    r"\bdoes\s+(@?[\w.\-]+?)\s+(?:exist\s+)?(?:on|in)\s+(?:this\s+|the\s+)?(?:server|guild|discord)\b",
    re.IGNORECASE,
)
MEMBER_LOOKUP_PATTERN_C = re.compile(
    r"\bis\s+there\s+(?:a\s+)?(?:user|member|person)\s+(?:named|called)\s+(@?[\w.\-]+)",
    re.IGNORECASE,
)

# 🌸 A bare pronoun slipping through one of the patterns above (e.g.
# "does HE on this server" — "he" lands in the capture group same as a
# real name would) is never an actual lookup-able name. Checked in
# handle_member_lookup_query below before querying the DB with garbage.
_MEMBER_LOOKUP_PRONOUN_STOPWORDS = frozenset({
    "he", "she", "they", "him", "her", "them", "it", "this", "that", "you", "i",
})


async def handle_member_lookup_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False, groq_client=None, name_hint: str | None = None) -> str | None:
    """
    🌸 "IS USER ON THIS SERVER" HANDLER — answers member-existence
    questions straight from the guild's cached member table via
    shared.find_member_by_name, the exact same lookup
    handle_user_role_query already uses for name→id resolution.

    🐛 BEFORE THIS EXISTED: there was no label/regex anywhere in the
    server-query pipeline for a bare existence question, so it fell all
    the way through to the general chat model — which has no access to
    real member data and would still confidently answer ("I don't see a
    user named X on this server") with NOTHING behind that claim.
    find_member_by_name itself was already fully built (added for
    handle_user_role_query) but never called from anywhere for this.
    This grounds the answer in the actual cache instead of a guess.

    Target resolution priority:
      1. message.mentions — an actual @mention, zero ambiguity, free.
      2. name_hint — an AI-resolved name from
         classifiers.extract_member_name (see groq_service.py's
         _try_ai), passed in when the classifier already labeled this
         message "member_lookup" but the CURRENT message alone has no
         literal name for a regex to find (e.g. "does he on this
         server" — the real name only lives in the folded reply-context
         dispatch_text the AI got to read, not message.content this
         handler's own regex sees). AI-first, same philosophy as the
         rest of this dispatch pipeline — takes priority over #3 below
         whenever it resolves to something that isn't itself a bare
         pronoun.
      3. MEMBER_LOOKUP_PATTERN_A/B/C matched against message.content —
         the free, zero-API-call fallback for when name_hint is absent
         (e.g. called directly from the regex chain, no AI in the loop
         at all) or came back empty.

    Fails open (returns None) at every step — no match, no client, a
    bare pronoun with nothing to resolve it, or any DB error — same
    "never worse than falling through" contract as the rest of this file.
    """
    target_name: str | None = None
    matches: list[dict] | None = None

    # 🌸 A mention of the bot ITSELF (e.g. "@EE does X exist", or a bare
    # "@EE" used just to address the bot) is not a lookup target — it's
    # how the user is talking TO the bot, not asking whether the bot is
    # on the server. Without this filter, any message that merely pings
    # the bot short-circuited straight to "Yep, EE is here on this
    # server!" regardless of what was actually being asked.
    real_mentions = [
        m for m in message.mentions
        if not (message.guild and message.guild.me and m.id == message.guild.me.id)
    ]

    if real_mentions:
        target = real_mentions[0]
        target_name = target.display_name or target.name
        matches = [{
            "id": target.id, "username": target.name,
            "display_name": target.display_name, "bot": target.bot,
        }]
    elif name_hint and name_hint.strip().lower() not in _MEMBER_LOOKUP_PRONOUN_STOPWORDS:
        target_name = name_hint.strip()
        try:
            matches = await shared.find_member_by_name(guild_id, target_name)
        except Exception as e:
            print(f"⚠️ Member lookup query error: {e}")
            return None
    else:
        for pattern in (MEMBER_LOOKUP_PATTERN_A, MEMBER_LOOKUP_PATTERN_B, MEMBER_LOOKUP_PATTERN_C):
            match = pattern.search(message.content)
            if match:
                target_name = match.group(1).strip().strip("@")
                break

        if not target_name:
            return None

        if target_name.lower() in _MEMBER_LOOKUP_PRONOUN_STOPWORDS:
            return None

        try:
            matches = await shared.find_member_by_name(guild_id, target_name)
        except Exception as e:
            print(f"⚠️ Member lookup query error: {e}")
            return None

    try:
        if not matches:
            raw_result = f"❌ Nope, I don't see anyone named **{target_name}** on this server."
        elif len(matches) == 1:
            m = matches[0]
            display = m.get("display_name") or m.get("username")
            raw_result = f"✅ Yep, **{display}** is here on this server! 🌸"
        else:
            # 🌸 2+ ambiguous matches — same convention as
            # handle_user_role_query: don't guess, surface the
            # candidates instead of a confident-sounding wrong answer.
            names = ", ".join(m.get("display_name") or m.get("username") for m in matches[:10])
            raw_result = f"Found a few people close to that — {names}. Which one did you mean?"

        return await maybe_rephrase_db_result(raw_result, message.content, groq_client, message.author.name)
    except Exception as e:
        print(f"⚠️ Member lookup query error: {e}")
        return None


# 🌸 SERVER CREATION DATE QUERY DETECTION — "when was this server created/made",
# "when was this server founded" — bypass Groq entirely, read straight from
# metadata.db's guild_info.created_at (populated by the v10 guild fetch).
CREATED_QUERY_PATTERN = re.compile(
    r"\bwhen\s+(?:was|is|did)\s+(?:this\s+|the\s+)?(?:server|guild)\s+(?:created|made|founded|started)\b",
    re.IGNORECASE
)


# 🌸 SERVER INFO QUERY DETECTION — "what is this server", "tell me about this server",
# "server info", "server details" — bypass Groq entirely, pull from metadata.db
SERVER_INFO_PATTERN = re.compile(
    r"\b(what|tell\s+me|show|give\s+me|describe)\s+(?:is|about|me)?\s*(?:this\s+)?(?:server|guild|place)\b",
    re.IGNORECASE
)


async def handle_server_info_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER INFO HANDLER — intercepts "what is this server" / "tell me about this server"
    before they hit Groq, pulls metadata straight from metadata.db.
    Returns a formatted overview, or None if regex doesn't match.

    skip_pattern_check: trust an AI classifier "server_info" label instead
    of re-gating on SERVER_INFO_PATTERN. See handle_created_query's
    docstring for the full reasoning.
    """
    if not skip_pattern_check:
        match = SERVER_INFO_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        if not meta:
            return None

        guild_name = meta.get("name", "Unknown Server")
        owner_id = meta.get("owner_id")
        member_count = meta.get("member_count", "?")
        description = meta.get("description")
        verification = meta.get("verification_level")
        icon_hash = meta.get("icon")

        lines = [f"**{guild_name}** 🏛️"]
        if description:
            lines.append(f"  {description}")
        lines.append(f"  👥 {member_count} members")
        if owner_id:
            lines.append(f"  👑 Owner ID: {owner_id}")
        if verification:
            lines.append(f"  🛡️ Verification: {str(verification).replace('_', ' ').title()}")
        if icon_hash:
            lines.append(f"  🖼️ Icon: {_cdn_image_url('icons', guild_id, icon_hash)}")

        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️ Server info query error: {e}")
        return None


# 🌸 SERVER DESCRIPTION QUERY DETECTION — "server description", "what's the
# description", "look at this server description" — a SPECIFIC field ask
# that used to get swallowed by the broad SERVER_INFO_PATTERN above (which
# fired on "look at this server..." too and dumped the WHOLE overview
# instead of answering the actual question). Kept separate from
# server_info/handle_server_info_query so classify_server_query can route
# "description" asks to a handler that answers ONLY the description field,
# with an honest "it's empty" instead of silently listing member count etc.
SERVER_DESCRIPTION_PATTERN = re.compile(
    r"\b(?:server\s+description|description\s+(?:of\s+)?(?:this\s+)?(?:server|guild)|"
    r"(?:what'?s?|show|look\s+at|check|read)\s+(?:the\s+|this\s+)?(?:server\s+)?description)\b",
    re.IGNORECASE
)


async def handle_server_description_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER DESCRIPTION HANDLER — intercepts "server description",
    "what's the description", "look at this server description" before
    they hit Groq or the generic server_info catch-all. Answers ONLY the
    description field, straight from metadata.db, instead of the AI
    guessing/hallucinating something plausible-sounding when the field is
    actually blank (most servers never set one — Discord's description
    field is opt-in, separate from the vanity/about text).

    skip_pattern_check: trust an AI classifier "description" label
    instead of re-gating on SERVER_DESCRIPTION_PATTERN.
    """
    if not skip_pattern_check:
        match = SERVER_DESCRIPTION_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        guild_name = (meta.get("name") if meta else None) or (message.guild.name if message.guild else "this server")
        description = meta.get("description") if meta else None

        if description:
            return f"📝 **{guild_name}**'s description:\n> {description}"
        return f"📝 **{guild_name}** doesn't have a description set yet — it's empty! 🌸"
    except Exception as e:
        print(f"⚠️ Server description query error: {e}")
        return None


# 🌸 ALL-METADATA QUERY DETECTION — "give me all metadata", "show everything
# about this server", "full server details/info", "dump server data" — an
# explicit ask for the COMPLETE metadata.db row, not just one field.
# Distinct from SERVER_INFO_PATTERN (which is a curated highlight reel of
# ~5 fields) — this one is a full field-by-field dump for people who
# specifically want "all"/"everything"/"full"/"complete".
ALL_METADATA_PATTERN = re.compile(
    r"\b(?:all|every|full|complete|entire)\s+(?:the\s+)?(?:server\s+)?(?:metadata|meta\s*data|details|info(?:rmation)?|data)\b"
    r"|\b(?:metadata|meta\s*data)\s+(?:of|for|about)\s+(?:this\s+)?(?:server|guild)\b"
    r"|\bshow\s+(?:me\s+)?everything\s+(?:about\s+)?(?:this\s+)?(?:server|guild)\b",
    re.IGNORECASE
)


async def handle_all_metadata_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX ALL-METADATA HANDLER — intercepts explicit "give me everything/
    all metadata/full details" asks before they hit Groq. Unlike
    handle_server_info_query (a curated ~5-field highlight reel), this
    dumps every field metadata.db actually has for the guild, field by
    field, so nothing gets left out just because it wasn't deemed
    "highlight-worthy" by the curated handler.

    skip_pattern_check: trust an AI classifier "all_metadata" label
    instead of re-gating on ALL_METADATA_PATTERN.
    """
    if not skip_pattern_check:
        match = ALL_METADATA_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        if not meta:
            return None

        guild_name = meta.get("name", "Unknown Server")
        lines = [f"📋 **{guild_name}** — full metadata:"]

        field_labels = [
            ("description", "📝 Description"),
            ("owner_id", "👑 Owner ID"),
            ("member_count", "👥 Members"),
            ("verification_level", "🛡️ Verification"),
            ("preferred_locale", "🌐 Locale"),
            ("boost_tier", "🚀 Boost Tier"),
            ("boost_count", "💎 Boosts"),
            ("created_at", "📅 Created"),
            ("vanity_url", "🔗 Vanity URL"),
            ("nsfw_level", "🔞 NSFW Level"),
            ("explicit_content_filter", "🔍 Content Filter"),
            ("features", "✨ Features"),
        ]

        for key, label in field_labels:
            value = meta.get(key)
            if value in (None, "", [], {}):
                continue
            if key == "verification_level":
                value = str(value).replace("_", " ").title()
            elif key == "features" and isinstance(value, list):
                value = ", ".join(value) if value else None
                if not value:
                    continue
            lines.append(f"  {label}: {value}")

        icon_hash = meta.get("icon")
        if icon_hash:
            lines.append(f"  🖼️ Icon: {_cdn_image_url('icons', guild_id, icon_hash)}")
        banner_hash = meta.get("banner")
        if banner_hash:
            lines.append(f"  🎨 Banner: {_cdn_image_url('banners', guild_id, banner_hash)}")

        if len(lines) == 1:
            lines.append("  (nothing else on file yet — cache may need a refresh 🌸)")

        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️ All-metadata query error: {e}")
        return None


# 🌸 CHANNEL COUNT QUERY DETECTION — "how many channels", "how much channel", "channel count", "list channels"
CHANNEL_COUNT_PATTERN = re.compile(
    r"\b(how\s+(?:many|much)|count|list|show\s+all)\s+(?:channels?|text\s+channels?|voice\s+channels?)\b",
    re.IGNORECASE
)


async def handle_channel_count_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX CHANNEL COUNT HANDLER — intercepts "how many channels", "list channels",
    "channel count" before they hit Groq, queries channels.db directly.
    Returns formatted channel list, or None if regex doesn't match.

    skip_pattern_check: trust an AI classifier "channel_count" label
    instead of re-gating on CHANNEL_COUNT_PATTERN.
    """
    if not skip_pattern_check:
        match = CHANNEL_COUNT_PATTERN.search(message.content)
        if not match:
            return None

    try:
        summary = await shared.get_compact_channels(guild_id, max_count=200)
        if "No channels" in summary or "Error" in summary:
            return None

        lines = [f"💬 **Channels in this server:**"]
        lines.extend(summary.split("\n"))
        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️ Channel count query error: {e}")
        return None


# 🌸 ROLE LIST QUERY DETECTION — "what roles", "list roles", "show roles"
ROLE_LIST_PATTERN = re.compile(
    r"\b(what|list|show|how\s+many)\s+(?:are\s+the\s+)?roles?\b",
    re.IGNORECASE
)


async def handle_role_list_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX ROLE LIST HANDLER — intercepts "what roles are there", "list all roles",
    "show roles" before they hit Groq, queries roles.db directly.
    Returns formatted role list with member counts, or None if regex doesn't match.

    skip_pattern_check: trust an AI classifier "role_list" label instead
    of re-gating on ROLE_LIST_PATTERN.
    """
    if not skip_pattern_check:
        match = ROLE_LIST_PATTERN.search(message.content)
        if not match:
            return None

    try:
        summary = await shared.get_compact_roles(guild_id, max_count=100)
        if "No roles" in summary or "Error" in summary:
            return None

        lines = [f"👥 **Roles in this server:**"]
        lines.extend(summary.split("\n"))
        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️ Role list query error: {e}")
        return None


async def handle_created_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER-CREATION-DATE HANDLER — intercepts "when was this server
    created" questions before they hit Groq, reads guild_info.created_at
    straight from metadata.db instead of guessing/hallucinating, and formats
    it into a friendly reply.

    Examples that match:
      • "when was this server created"
      • "when did this server get made"
      • "when was this server founded"

    skip_pattern_check: when True, the CREATED_QUERY_PATTERN regex gate
    below is skipped entirely — used when classify_server_query already
    labeled the message "created"/"age" with reply-context it could see
    that this function can't (this function only ever looks at
    message.content). Without this, phrasing the classifier correctly
    recognized (e.g. "when did this server WAS made", "when did this
    discord server was created" — non-native-speaker word order the
    regex's rigid server+created adjacency can't cover) would still get
    rejected here and silently fall through to the hallucinating chat
    model, defeating the whole point of the AI-first classifier.

    Returns a formatted date string. Never returns None just because
    metadata.db hasn't been synced yet — a guild ID is a Discord snowflake,
    which encodes its creation timestamp in its first 42 bits, so the real
    date is always derivable locally with zero API calls and zero chance
    of the AI guessing/hallucinating a wrong one (see the July 2026 bug
    where a stale/empty cache row made Groq say "idk the exact date" and
    invent a workaround instead of just answering).
    """
    if not skip_pattern_check:
        match = CREATED_QUERY_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        created_at_raw = meta.get("created_at")
        guild_name = meta.get("name") or (message.guild.name if message.guild else "this server")

        created_dt = None
        if created_at_raw:
            try:
                created_dt = datetime.fromisoformat(created_at_raw)
            except ValueError:
                created_dt = None  # fall through to snowflake decode below

        if created_dt is None:
            # 🌸 metadata.db is missing/stale for this guild — decode the
            # timestamp straight from the snowflake ID instead of bailing.
            created_dt = discord.utils.snowflake_time(guild_id)

        pretty_date = created_dt.strftime("%B %d, %Y")
        return f"🏛️ **{guild_name}** was created on **{pretty_date}** 🌸"
    except Exception as e:
        print(f"⚠️ Created-date query error: {e}")
        return None

# 🌸 USER ACCOUNT-CREATION-DATE QUERY DETECTION — "when was my account
# created", "when did I make my discord account", "how old is my account" —
# bypass Groq entirely and decode straight from the message author's own
# snowflake ID. Also supports asking about someone ELSE via a mention.
USER_CREATED_QUERY_PATTERN = re.compile(
    r"\bwhen\s+(?:was|is|did)\s+(?:my|his|her|their|this)\s+(?:discord\s+)?account\s+(?:was\s+|is\s+|did\s+)?(?:created|made|born|started)\b"
    r"|\bhow\s+old\s+is\s+(?:my|his|her|their|this)\s+(?:discord\s+)?account\b",
    re.IGNORECASE
)

async def handle_user_created_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX USER-ACCOUNT-AGE HANDLER — intercepts "when was my account
    created" style questions before they hit Groq.

    Unlike handle_created_query (server), this needs ZERO database lookups:
    a Discord user ID IS a snowflake, and a snowflake's first 42 bits ARE
    the creation timestamp — discord.utils.snowflake_time() decodes it
    locally with no API call and no chance of staleness.

    Examples that match:
      • "when was my account created"
      • "when did I make my discord account"
      • "how old is my account"

    If the message @mentions someone, answers about THEM instead of the
    author (e.g. "when was @someone's account created").

    skip_pattern_check: trust an AI classifier "user_created" label
    instead of re-gating on USER_CREATED_QUERY_PATTERN — the mention
    target is read from message.mentions either way, not from the regex
    match, so this is safe to skip.
    """
    if not skip_pattern_check:
        match = USER_CREATED_QUERY_PATTERN.search(message.content)
        if not match:
            return None

    try:
        # 🌸 Exclude the bot itself from mentions — pinging @Cutest Thing to
        # ASK the question is not the same as asking ABOUT Cutest Thing's
        # account. Only a mention of someone ELSE redirects the target.
        real_mentions = [u for u in message.mentions if not u.bot]
        target = real_mentions[0] if real_mentions else message.author

        created_dt = discord.utils.snowflake_time(target.id)
        pretty_date = created_dt.strftime("%B %d, %Y")
        pretty_time = created_dt.strftime("%I:%M %p UTC")

        who = "Your" if target.id == message.author.id else f"**{target.display_name}**'s"
        return f"🎂 {who} Discord account was created on **{pretty_date}** at **{pretty_time}** 🌸"
    except Exception as e:
        print(f"⚠️ User account-created query error: {e}")
        return None 

# ─────────────────────────────────────────────────────────────────────────────
# 🌸 SERVER CONTEXT — lets Groq know which Discord server it's replying in,
# pulled straight from the v10 REST API (GET /guilds/{id}) rather than
# something the user has to explain themselves. Same idea as
# IDENTITY_INSTRUCTIONS but for "where am I" instead of "who am I
# talking to". See EnchantedBot._fetch_guild_v10 / get_server_context_text.
# ─────────────────────────────────────────────────────────────────────────────

# 🌸 How long a fetched guild's basic info stays cached before the next
# mention in that server triggers a fresh REST call. Server name/member
# count/boost tier barely change turn to turn, so there's no reason to hit
# the API every single message.
GUILD_INFO_CACHE_TTL = 600  # 10 minutes

# 🌸 The full v10 guild object's features[] array can have 20+ entries on a
# big server (a lot of them internal plumbing like "TICKETED_EVENTS_ENABLED"
# that mean nothing to the AI) — only these get surfaced in server context.
NOTABLE_GUILD_FEATURES = {
    "COMMUNITY":    "Community server",
    "PARTNERED":    "Discord Partner",
    "VERIFIED":     "Verified server",
    "DISCOVERABLE": "Discoverable in Server Discovery",
}

_VERIFICATION_LEVELS = {0: "None", 1: "Low", 2: "Medium", 3: "High", 4: "Highest"}
_NSFW_LEVELS         = {0: "Default", 1: "Explicit", 2: "Safe", 3: "Age Restricted"}


def _strip_guild_json(raw: dict) -> dict:
    """
    🌸 Takes the FULL Discord v10 GET /guilds/{id} payload — which includes
    big arrays like roles[], emojis[], stickers[], a welcome_screen object,
    and a bunch of raw channel-ID/permission fields the AI has zero use
    for — and keeps only the handful of "basic info" fields worth telling
    Groq about. This is the "strip away unnecessary JSON" step.
    """
    guild_id   = str(raw.get("id") or "")
    created_at = None
    if guild_id.isdigit():
        created_ms = (int(guild_id) >> 22) + DISCORD_EPOCH_MS
        created_at = datetime.utcfromtimestamp(created_ms / 1000).strftime("%Y-%m-%d")

    features = [
        label for flag, label in NOTABLE_GUILD_FEATURES.items()
        if flag in (raw.get("features") or [])
    ]

    return {
        "name":         raw.get("name") or "Unknown Server",
        "description":  raw.get("description") or None,
        "owner_id":     raw.get("owner_id"),
        "member_count": raw.get("approximate_member_count"),
        "online_count": raw.get("approximate_presence_count"),
        "boost_tier":   raw.get("premium_tier", 0),
        "boost_count":  raw.get("premium_subscription_count", 0),
        "verification": _VERIFICATION_LEVELS.get(raw.get("verification_level"), "Unknown"),
        "nsfw_level":   _NSFW_LEVELS.get(raw.get("nsfw_level"), "Default"),
        "locale":       raw.get("preferred_locale") or "en-US",
        "vanity_url":   raw.get("vanity_url_code"),
        "created_at":   created_at,
        "features":     features,
    }


SERVER_CONTEXT_INSTRUCTIONS = (
    "You already know this — never ask what server this is or claim you "
    "don't know where you're chatting.\n"
    "Server: \"{name}\"{vanity_part}\n"
    "{description_part}"
    "Owner: <@{owner_id}> (ID: {owner_id})\n"
    "Members: ~{member_count}{online_part}\n"
    "Boost level: {boost_tier}{boost_count_part}\n"
    "Created: {created_at}\n"
    "{features_part}"
)

# 🌸 Shown instead of SERVER_CONTEXT_INSTRUCTIONS when guild is None (DMs
# have no server to fetch/describe).
DM_CONTEXT_INSTRUCTIONS = (
    "You're in a private DM right now, not a server — there's no server "
    "to reference here."
)


# ─────────────────────────────────────────────────────────────────────────────
# 🌸 SERVER AVATAR / ICON QUERY — "give me avatar of this server", "server icon",
# "what's the server pfp/logo" — bypass Groq entirely (it has no way to fetch or
# link an image on its own, which is why it used to just apologize) and build the
# CDN URL straight from the cached icon hash in metadata.db (falling back to the
# LIVE discord.py Guild object if metadata.db hasn't synced yet).
# ─────────────────────────────────────────────────────────────────────────────
SERVER_AVATAR_PATTERN = re.compile(
    r"\b(?:server|guild|this)\s+(?:avatar|icon|pfp|logo|image|picture)\b"
    r"|\b(?:avatar|icon|pfp|logo)\s+(?:of\s+)?(?:this\s+|the\s+)?(?:server|guild)\b"
    r"|\bserver\s+(?:avatar|icon|pfp)\b"
    r"|\bguild\s+(?:avatar|icon|pfp)\b",
    re.IGNORECASE
)


def _cdn_image_url(kind: str, snowflake_id: int, image_hash: str, size: int = 4096) -> str:
    """🌸 Builds a Discord CDN URL for an icon/banner hash. Animated hashes
    (prefixed 'a_') get a .gif, everything else gets .png — same convention
    discord.py's own Asset class uses under the hood."""
    ext = "gif" if image_hash.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/{kind}/{snowflake_id}/{image_hash}.{ext}?size={size}"


async def handle_server_avatar_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER AVATAR HANDLER — intercepts "give me avatar of this
    server", "server icon", "what's the server pfp/logo" before they hit
    Groq. Reads the icon hash from metadata.db (populated by sync_guild_to_db)
    and builds the CDN link directly — falls back to the live discord.py
    guild.icon if the cache hasn't synced yet, so it never has to say
    "I can't fetch that" the way the raw Groq reply used to.

    skip_pattern_check: trust an AI classifier "avatar" label instead of
    re-gating on SERVER_AVATAR_PATTERN.
    """
    if not skip_pattern_check:
        match = SERVER_AVATAR_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        guild_name = (meta.get("name") if meta else None) or (message.guild.name if message.guild else "this server")
        icon_hash = meta.get("icon") if meta else None

        icon_url = None
        if icon_hash:
            icon_url = _cdn_image_url("icons", guild_id, icon_hash)
        elif message.guild and message.guild.icon:
            icon_url = message.guild.icon.with_size(1024).url

        if not icon_url:
            return f"😔 **{guild_name}** doesn't have a server icon set right now!"

        return icon_url
    except Exception as e:
        print(f"⚠️ Server avatar query error: {e}")
        return None


# 🌸 SERVER BANNER QUERY — same idea as avatar, just the banner image.
SERVER_BANNER_PATTERN = re.compile(
    r"\bbanner\b(?:\s+\w+){0,4}\s+\b(?:this\s+|the\s+)?(?:server|guild)\b"
    r"|\b(?:server|guild)\b(?:\s+\w+){0,4}\s+\bbanner\b",
    re.IGNORECASE
)


async def handle_server_banner_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER BANNER HANDLER — intercepts "server banner", "what's
    this server's banner" before they hit Groq. Same cache-then-live-
    fallback pattern as handle_server_avatar_query.

    skip_pattern_check: trust an AI classifier "banner" label instead of
    re-gating on SERVER_BANNER_PATTERN.
    """
    if not skip_pattern_check:
        match = SERVER_BANNER_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        guild_name = (meta.get("name") if meta else None) or (message.guild.name if message.guild else "this server")
        banner_hash = meta.get("banner") if meta else None

        banner_url = None
        if banner_hash:
            banner_url = _cdn_image_url("banners", guild_id, banner_hash)
        elif message.guild and message.guild.banner:
            banner_url = message.guild.banner.with_size(1024).url

        if not banner_url:
            return f"😔 **{guild_name}** doesn't have a server banner set right now!"

        return f"🎨 **{guild_name}**'s server banner:\n{banner_url}"
    except Exception as e:
        print(f"⚠️ Server banner query error: {e}")
        return None


# 🌸 SERVER OWNER QUERY — "who owns this server", "who's the owner", "who made this server"
SERVER_OWNER_PATTERN = re.compile(
    r"\bwho\s+(?:is|owns|made|created|founded)\s+(?:the\s+|this\s+)?(?:server|guild)\b"
    r"|\bserver\s+owner\b"
    r"|\bwho'?s\s+the\s+owner\b",
    re.IGNORECASE
)


async def handle_server_owner_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER OWNER HANDLER — intercepts "who owns this server",
    "who's the owner", "server owner" before they hit Groq. Reads owner_id
    straight from metadata.db and pings them directly (mentions read way
    nicer than a raw ID, which is all Groq would've had to guess from).

    skip_pattern_check: trust an AI classifier "owner" label instead of
    re-gating on SERVER_OWNER_PATTERN.
    """
    if not skip_pattern_check:
        match = SERVER_OWNER_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        owner_id = meta.get("owner_id") if meta else None
        guild_name = (meta.get("name") if meta else None) or (message.guild.name if message.guild else "this server")

        if not owner_id and message.guild:
            owner_id = message.guild.owner_id

        if not owner_id:
            return f"😔 I don't have owner info cached for **{guild_name}** yet!"

        return f"👑 **{guild_name}** is owned by <@{owner_id}> (ID: `{owner_id}`)"
    except Exception as e:
        print(f"⚠️ Server owner query error: {e}")
        return None


# 🌸 SERVER VERIFICATION LEVEL QUERY — "verification level", "how verified is this server"
SERVER_VERIFICATION_PATTERN = re.compile(
    r"\bverification\s+level\b"
    r"|\bhow\s+verified\s+is\s+(?:this\s+|the\s+)?(?:server|guild)\b",
    re.IGNORECASE
)


async def handle_server_verification_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER VERIFICATION HANDLER — intercepts "verification level"
    / "how verified is this server" before they hit Groq, reads
    verification_level straight from metadata.db.

    skip_pattern_check: trust an AI classifier "verification" label
    instead of re-gating on SERVER_VERIFICATION_PATTERN.
    """
    if not skip_pattern_check:
        match = SERVER_VERIFICATION_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        if not meta:
            return None

        guild_name = meta.get("name") or (message.guild.name if message.guild else "this server")
        level = (meta.get("verification_level") or "unknown").replace("_", " ").title()

        return f"🛡️ **{guild_name}**'s verification level is **{level}**"
    except Exception as e:
        print(f"⚠️ Server verification query error: {e}")
        return None


# 🌸 MEMBER COUNT QUERY — "how many members", "member count", "how big is this server"
MEMBER_COUNT_PATTERN = re.compile(
    r"\b(?:how\s+(?:many|much)|count|total)\s+(?:of\s+)?members?\b"
    r"|\bhow\s+big\s+is\s+(?:this\s+|the\s+)?(?:server|guild)\b"
    r"|\bserver\s+(?:population|size)\b",
    re.IGNORECASE
)


async def handle_member_count_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX MEMBER COUNT HANDLER — intercepts "how many members", "member count",
    "how big is this server" before they hit Groq, reads straight from metadata.db.

    skip_pattern_check: trust an AI classifier "member_count" label
    instead of re-gating on MEMBER_COUNT_PATTERN.
    """
    if not skip_pattern_check:
        match = MEMBER_COUNT_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        if not meta:
            return None

        guild_name = meta.get("name") or (message.guild.name if message.guild else "this server")
        member_count = meta.get("member_count") or "?"

        return f"👥 **{guild_name}** has **{member_count}** members!"
    except Exception as e:
        print(f"⚠️ Member count query error: {e}")
        return None


# 🌸 SERVER AGE QUERY — "how old is this server", "server age", "when was it created"
SERVER_AGE_PATTERN = re.compile(
    r"\bhow\s+old\s+is\s+(?:this\s+|the\s+)?(?:server|guild)\b"
    r"|\bserver\s+age\b"
    r"|\bhow\s+(?:long|old)\s+has\s+this\s+(?:server|guild)\s+(?:been|existed)\b",
    re.IGNORECASE
)


async def handle_server_age_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX SERVER AGE HANDLER — intercepts "how old is this server", "server age"
    before they hit Groq. Calculates years/months from created_at in metadata.db.

    skip_pattern_check: when True, SERVER_AGE_PATTERN is skipped — used
    when classify_server_query already labeled the message "age" using
    context (or phrasing) this narrow regex doesn't cover. See
    handle_created_query's matching docstring note for the full reasoning.
    """
    if not skip_pattern_check:
        match = SERVER_AGE_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        guild_name = (meta.get("name") if meta else None) or (message.guild.name if message.guild else "this server")
        created_at_raw = meta.get("created_at") if meta else None

        created_dt = None
        if created_at_raw:
            try:
                created_dt = datetime.fromisoformat(created_at_raw)
            except ValueError:
                pass

        if created_dt is None and message.guild:
            # Fallback to snowflake decode
            created_dt = discord.utils.snowflake_time(guild_id)

        if not created_dt:
            return f"😔 I don't have creation date for **{guild_name}** cached yet!"

        now = datetime.now(created_dt.tzinfo) if created_dt.tzinfo else datetime.utcnow()
        delta = now - created_dt
        years = delta.days // 365
        months = (delta.days % 365) // 30

        age_str = ""
        if years > 0:
            age_str += f"{years} year{'s' if years != 1 else ''}"
        if months > 0:
            if age_str:
                age_str += f" and {months} month{'s' if months != 1 else ''}"
            else:
                age_str = f"{months} month{'s' if months != 1 else ''}"
        
        if not age_str:
            age_str = "less than a month"

        # 🌸 Always include the exact decoded date too, not just relative
        # age — "how old" and "what's the exact date" are different asks,
        # but created_dt (DB value or snowflake-decoded fallback) is
        # already sitting right here, so there's no reason to make the
        # user ask handle_created_query separately just to get it.
        pretty_date = created_dt.strftime("%B %d, %Y")
        return f"📅 **{guild_name}** is **{age_str}** old — created on **{pretty_date}**! 🌸"
    except Exception as e:
        print(f"⚠️ Server age query error: {e}")
        return None


# 🌸 BOOST/NITRO STATUS QUERY — "boost status", "boost tier", "nitro status"
BOOST_STATUS_PATTERN = re.compile(
    r"\bboost\s+(?:status|level|tier)\b"
    r"|\bnitro\s+(?:boosts?|status)\b"
    r"|\bhow\s+many\s+boosts?\b"
    r"|\bserver\s+boost(?:s|ed)?\b",
    re.IGNORECASE
)


async def handle_boost_status_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX BOOST STATUS HANDLER — intercepts "boost status", "boost tier", "nitro status"
    before they hit Groq. Reads boost_tier and boost_count from metadata.db.
    Note: These fields may not be populated yet if metadata schema doesn't include them —
    this will need schema update to include boost_tier, boost_count.

    skip_pattern_check: trust an AI classifier "boost" label instead of
    re-gating on BOOST_STATUS_PATTERN.
    """
    if not skip_pattern_check:
        match = BOOST_STATUS_PATTERN.search(message.content)
        if not match:
            return None

    try:
        # For now, we'll try to get it from the live guild object since metadata.db
        # may not have these fields persisted yet. TODO: Update save_guild_db and
        # metadata.db schema to include boost_tier and boost_count.
        guild_name = (message.guild.name if message.guild else None) or "this server"
        
        if message.guild:
            tier = message.guild.premium_tier
            boost_count = message.guild.premium_subscription_count or 0
            
            tier_names = {
                0: "None",
                1: "Tier 1",
                2: "Tier 2",
                3: "Tier 3",
            }
            tier_name = tier_names.get(tier, "Unknown")
            
            return f"🚀 **{guild_name}** is at **Boost {tier_name}** with **{boost_count}** boosts! 💖"
        
        return None
    except Exception as e:
        print(f"⚠️ Boost status query error: {e}")
        return None


# 🌸 LOCALE/LANGUAGE QUERY — "what language", "server language", "locale"
LOCALE_PATTERN = re.compile(
    r"\b(?:what\s+)?(?:language|locale)\b(?:\s+(?:is|does)\s+(?:this\s+)?(?:server|guild))?",
    re.IGNORECASE
)


async def handle_locale_query(message: discord.Message, guild_id: int, shared, skip_pattern_check: bool = False) -> str | None:
    """
    🌸 REGEX LOCALE HANDLER — intercepts "what language", "server language", "locale"
    before they hit Groq. Reads preferred_locale from metadata.db.

    skip_pattern_check: trust an AI classifier "locale" label instead of
    re-gating on LOCALE_PATTERN.
    """
    if not skip_pattern_check:
        match = LOCALE_PATTERN.search(message.content)
        if not match:
            return None

    try:
        meta = await shared.get_guild_metadata(guild_id)
        if not meta:
            return None

        guild_name = meta.get("name") or (message.guild.name if message.guild else "this server")
        locale = meta.get("preferred_locale") or "en-US (English)"

        # Friendly locale names
        locale_names = {
            "en-US": "🇺🇸 English (US)",
            "en-GB": "🇬🇧 English (UK)",
            "ja": "🇯🇵 Japanese",
            "zh-CN": "🇨🇳 Chinese (Simplified)",
            "zh-TW": "🇹🇼 Chinese (Traditional)",
            "de": "🇩🇪 German",
            "es-ES": "🇪🇸 Spanish (Spain)",
            "es-419": "🇮🇳 Spanish (Latin America)",
            "fr": "🇫🇷 French",
            "it": "🇮🇹 Italian",
            "ko": "🇰🇷 Korean",
            "pt-BR": "🇧🇷 Portuguese (Brazil)",
            "pt": "🇵🇹 Portuguese",
            "ru": "🇷🇺 Russian",
            "th": "🇹🇭 Thai",
            "tr": "🇹🇷 Turkish",
            "uk": "🇺🇦 Ukrainian",
            "vi": "🇻🇳 Vietnamese",
            "pl": "🇵🇱 Polish",
            "nl": "🇳🇱 Dutch",
            "sv-SE": "🇸🇪 Swedish",
            "no": "🇳🇴 Norwegian",
            "da": "🇩🇰 Danish",
            "fi": "🇫🇮 Finnish",
        }
        
        friendly = locale_names.get(locale, locale)

        return f"🌐 **{guild_name}**'s preferred language is **{friendly}**"
    except Exception as e:
        print(f"⚠️ Locale query error: {e}")
        return None


def _format_server_context(info: dict) -> str:
    """🌸 Turns a _strip_guild_json()-shaped dict into the short text
    blurb spliced into Groq's system prompt."""
    vanity_part       = f" (discord.gg/{info['vanity_url']})" if info.get("vanity_url") else ""
    description_part  = f"About: {info['description']}\n" if info.get("description") else ""
    online_part       = f" (~{info['online_count']} online now)" if info.get("online_count") else ""
    boost_count_part  = f" ({info['boost_count']} boosts)" if info.get("boost_count") else ""
    features_part     = f"Notable: {', '.join(info['features'])}\n" if info.get("features") else ""

    return SERVER_CONTEXT_INSTRUCTIONS.format(
        name=info.get("name", "Unknown Server"),
        vanity_part=vanity_part,
        description_part=description_part,
        owner_id=info.get("owner_id") or "unknown",
        member_count=info.get("member_count") or "?",
        online_part=online_part,
        boost_tier=info.get("boost_tier", 0),
        boost_count_part=boost_count_part,
        created_at=info.get("created_at") or "unknown",
        features_part=features_part,
    ).strip()
