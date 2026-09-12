import re

# 🌸 Label -> handler map. Each label corresponds 1:1 to one of the
# handle_*_query functions above/below. "none" means "not a server-info
# question" (or classifier couldn't decide) — falls through to the
# regular regex chain -> Groq chat model, same as today.
SERVER_QUERY_LABELS = (
    "avatar", "banner", "owner", "verification", "member_count",
    "age", "boost", "locale", "description", "all_metadata", "server_info",
    "channel_count", "role_list", "role_query", "user_role_query",
    "member_lookup", "created", "user_created", "none",
)

# 🌸 CHEAP LOCAL PRE-FILTER — gates whether classify_server_query (a real
# Groq API call: full policy as input tokens + a label as output tokens)
# is even worth making. Before this existed, EVERY mention triggered that
# call, including things like "ur cute 🥺" or "lol how are u" that have
# zero chance of being server-related — pure wasted tokens + latency on
# the vast majority of messages, which are just casual chat.
#
# This is deliberately generous/over-inclusive (a superset of everything
# the 16 labels above could plausibly refer to, plus loose synonyms like
# "discord"/"place"/"guild") — false positives just mean the classifier
# runs and confidently returns "none", exactly what happens today. False
# NEGATIVES are the only real risk (a paraphrase the classifier would've
# caught getting skipped entirely), so the bar for "does this look
# server-ish at all" is kept low on purpose.
SERVER_QUERY_HINT_PATTERN = re.compile(
    r"\b(server|guild|discord|place|channel|role|member|avatar|banner|"
    r"icon|pfp|owner|owns|created|founded|made|started|verification|"
    r"boost|nitro|locale|language|region|description|about|info|"
    r"metadata|details|account|old|age)\b",
    re.IGNORECASE
)


def _looks_server_related(content: str) -> bool:
    """🌸 Zero-token local gate — True if the message contains ANY word
    that could plausibly relate to a server-info question. Only when this
    is True do we spend a Groq call on classify_server_query; otherwise
    we skip straight to the regular regex chain / chat model, saving a
    full classifier round-trip on the majority of casual mentions."""
    return bool(content) and bool(SERVER_QUERY_HINT_PATTERN.search(content))

# 🌸 Kept intentionally short — every extra sentence here is extra input
# tokens on EVERY single mention, not just server-info ones. One line per
# label, no examples, no chain-of-thought instructions (this model isn't
# a reasoning model, so asking it to "think step by step" would just add
# output tokens for no benefit).
#
# 🌸 PRIORITY ORDER matters here: specific single-field labels (avatar,
# banner, owner, verification, member_count, age, boost, locale,
# description) must always win over the two catch-alls (server_info,
# all_metadata) when a message could plausibly match both — e.g. "look at
# this server description" mentions "server" AND "description", but the
# person wants ONLY the description field, not the whole curated overview.
# The policy spells this out explicitly instead of relying on label order
# in the tuple, since the classifier model reads prompt text, not Python.
SERVER_QUERY_CLASSIFIER_POLICY = (
    "Classify a Discord message about THIS server/guild into exactly one label.\n"
    "PRIORITY: if a SPECIFIC field is named (description, avatar, banner, "
    "owner, verification, member count, age, boost, locale), pick THAT "
    "label even if generic words like \"server\"/\"about\"/\"info\" also "
    "appear. Only use server_info or all_metadata when NO specific field "
    "is named.\n"
    "avatar=server icon/pfp. banner=server banner image. owner=who owns/created-by. "
    "verification=verification level. member_count=how many members. "
    "age=how old/when created/when made/when founded (server) — this "
    "includes awkward or non-native phrasing like \"when did this server "
    "was made\" or \"when did this discord server was created\", classify "
    "those as age too. boost=boost/nitro status. "
    "locale=server language/region. description=asking specifically for "
    "the server's description/about text (e.g. \"server description\", "
    "\"what's the description\", \"look at the description\"). "
    "all_metadata=explicitly wants EVERYTHING/ALL/FULL/COMPLETE server "
    "metadata dumped, not just a highlight. server_info=general \"what is "
    "this server\" with NO specific field named. "
    "channel_count=how many channels. role_list=list all roles. "
    "role_query=asking who/which MEMBERS have a SPECIFIC named role — "
    "role is the subject, answer is a list of people (e.g. \"who has the "
    "admin role\", \"who has role of P\", \"which members have Mod\"). "
    "user_role_query=asking whether one SPECIFIC named/mentioned USER has "
    "a specific role — a person is the subject, answer is yes/no for "
    "that one person (e.g. \"does @bob have the admin role\", \"does "
    "its.cuteee_ has role p\", \"is Sarah a moderator\"). If a specific "
    "person AND a specific role are both named, prefer user_role_query "
    "over role_query. member_lookup=asking whether a SPECIFIC named/"
    "mentioned person IS ON or A MEMBER OF this server AT ALL — pure "
    "existence, not a role (e.g. \"is moonaesthetic0435 on this server\", "
    "\"does @bob exist in this server\", \"is there a user named X\"). "
    "If the message asks about a ROLE, prefer user_role_query/role_query "
    "instead. created=when server created "
    "(duplicate of age, prefer age). user_created=when a specific USER's "
    "account/join date was created. none=not about this server/guild at "
    "all, INCLUDING opinions/compliments/comments about the server that "
    "aren't asking for any data (e.g. \"is that a cool server\", \"nice "
    "server\", \"i like this server\") — those are none even though they "
    "contain the word \"server\".\n"
    "Reply with EXACTLY one label word, nothing else."
)
