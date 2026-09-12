"""🌸 groq_instruct — facade.

Bodies live in instruct/*.py. Keep this file so existing
`from groq_instruct import ...` imports still work.

Expected layout next to this file:

    groq_instruct.py
    instruct/
        canned.py
        server_queries.py
        routing.py
        models_search.py
        safety_policy.py
        react_identity.py
        reasoning.py
"""

from instruct.canned import (
    CONTEXT_TRIGGERS,
    KEYWORD_STOPWORDS,
)
from instruct.routing import (
    SERVER_QUERY_LABELS,
    SERVER_QUERY_HINT_PATTERN,
    SERVER_QUERY_CLASSIFIER_POLICY,
    _looks_server_related,
)
from instruct.models_search import (
    MODEL_POOL,
    CLASSIFIER_MODEL,
    SEARCH_INTENT_PATTERN,
    SEARCH_INTENT_CLASSIFIER_POLICY,
    SEARCH_TOPIC_DOMAINS,
    _wants_web_search,
    _search_domains_for_prompt,
)
from instruct.reasoning import (
    THINK_BLOCK_PATTERN,
    THINK_TAG_PATTERN,
    _strip_reasoning,
)
from instruct.safety_policy import (
    SAFEGUARD_MODEL,
    SAFEGUARD_POLICY,
    OUTPUT_SAFEGUARD_POLICY,
    _SEVERE_TERMS_NORMALIZED,
    _LEET_MAP,
    _normalize_for_filter,
    _contains_severe_term,
    _try_decode_base64,
    _check_base64_for_severe_terms,
)
from instruct.react_identity import (
    CREATOR_ID,
    REACT_TAG_PATTERN,
    REACT_REQUEST_PATTERN,
    EXPLICIT_EMOJI_PATTERN,
    AUTO_REACT_CHANCE,
    REACT_EMOJI_POOL,
    RECENT_EMOJI_MEMORY,
    _build_react_instructions,
    REACT_INSTRUCTIONS,
    REACT_INSTRUCTIONS_DISALLOWED,
    EMOJI_NAME_MAP,
    IDENTITY_INSTRUCTIONS,
    _build_owner_status,
)
from instruct.server_queries import (
    DISCORD_EPOCH_MS,
    _ROLE_VERB,
    _WHO_WHICH_SUBJECT,
    ROLE_QUERY_PATTERN,
    ROLE_QUERY_PATTERN_ROLE_FIRST,
    USER_ROLE_QUERY_PATTERN,
    REPHRASE_CHANCE,
    maybe_rephrase_db_result,
    handle_role_query,
    handle_user_role_query,
    MEMBER_LOOKUP_PATTERN_A,
    MEMBER_LOOKUP_PATTERN_B,
    MEMBER_LOOKUP_PATTERN_C,
    _MEMBER_LOOKUP_PRONOUN_STOPWORDS,
    handle_member_lookup_query,
    CREATED_QUERY_PATTERN,
    handle_created_query,
    SERVER_INFO_PATTERN,
    handle_server_info_query,
    SERVER_DESCRIPTION_PATTERN,
    handle_server_description_query,
    ALL_METADATA_PATTERN,
    handle_all_metadata_query,
    CHANNEL_COUNT_PATTERN,
    handle_channel_count_query,
    ROLE_LIST_PATTERN,
    handle_role_list_query,
    USER_CREATED_QUERY_PATTERN,
    handle_user_created_query,
    GUILD_INFO_CACHE_TTL,
    NOTABLE_GUILD_FEATURES,
    _VERIFICATION_LEVELS,
    _NSFW_LEVELS,
    _strip_guild_json,
    SERVER_CONTEXT_INSTRUCTIONS,
    DM_CONTEXT_INSTRUCTIONS,
    SERVER_AVATAR_PATTERN,
    _cdn_image_url,
    handle_server_avatar_query,
    SERVER_BANNER_PATTERN,
    handle_server_banner_query,
    SERVER_OWNER_PATTERN,
    handle_server_owner_query,
    SERVER_VERIFICATION_PATTERN,
    handle_server_verification_query,
    MEMBER_COUNT_PATTERN,
    handle_member_count_query,
    SERVER_AGE_PATTERN,
    handle_server_age_query,
    BOOST_STATUS_PATTERN,
    handle_boost_status_query,
    LOCALE_PATTERN,
    handle_locale_query,
    _format_server_context,
)
