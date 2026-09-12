import re

# 🌸 Context-aware mention replies — checked (in this order) BEFORE falling
# back to the random message roulette. Whole-word matching via regex so
# "hi" doesn't false-positive inside words like "history".
# More specific/rare phrasings are checked first so a message that touches
# multiple categories (e.g. "thanks, bye!") lands on the most meaningful one.
CONTEXT_TRIGGERS = {
    "love": {
        "pattern": re.compile(r"\b(i\s*love\s*you|ily|ur\s*cute|you'?re\s*cute|cute\s*bot)\b", re.IGNORECASE),
        "replies": [
            "Aww I love you too! 🌸💕",
            "Stoppp you're making me blush! 🎀✨",
            "That's so sweet of you! 💖",
        ],
    },
    "help": {
        "pattern": re.compile(r"\b(help|what\s*can\s*you\s*do|commands?)\b", re.IGNORECASE),
        "replies": [
            "Try `/help` to see everything I can do! 🌸✨",
            "I've got tons of commands — check `/help` for the full list! 💖",
        ],
    },
    "how_are_you": {
        "pattern": re.compile(r"\bhow\s*(are|r)\s*(you|u|ya)\b", re.IGNORECASE),
        "replies": [
            "I'm doing great, thanks for asking! 🌸 How about you?",
            "Feeling sparkly today! ✨ How are YOU doing?",
            "All good over here! 💖 What about you?",
        ],
    },
    "thanks": {
        "pattern": re.compile(r"\b(thanks?|thank\s*you|ty|thx|appreciate)\b", re.IGNORECASE),
        "replies": [
            "You're welcome! 🌸💖",
            "Anytime! ✨ That's what I'm here for!",
            "Aww, no problem at all! 🎀",
        ],
    },
    "farewell": {
        "pattern": re.compile(r"\b(bye+|goodbye|cya|see\s*ya|good\s*night|gn)\b", re.IGNORECASE),
        "replies": [
            "Bye bye! 🌸 Take care, okay?",
            "See ya~ 💖 Come back soon!",
            "Goodnight! 🌙✨ Sleep well!",
        ],
    },
    "greeting": {
        "pattern": re.compile(r"\b(hi+|hello+|hey+|heya|yo|sup|howdy|good\s*morning|gm)\b", re.IGNORECASE),
        "replies": [
            "Hii there! 🌸✨ How's your day going?",
            "Hello hello! 💖 What's up?",
            "Heyyy! 🎀 Good to see you!",
            "Hi hi~ 🌸 What can I do for you today?",
        ],
    },
}

# 🌸 Common filler/stopwords excluded when pulling keywords out of a mention
# message for the GENERIC keyword-overlap roulette (used when the message
# doesn't match any curated CONTEXT_TRIGGERS category above). Keeps matches
# meaningful instead of firing on "that", "with", "just", etc.
KEYWORD_STOPWORDS = frozenset({
    "that", "this", "with", "from", "have", "just", "your", "about", "what",
    "when", "where", "which", "would", "could", "should", "there", "their",
    "then", "than", "them", "they", "been", "being", "were", "will", "cutest",
    "thing", "really", "like", "some", "much", "very", "also", "into",
    "because", "even", "still", "only", "doing", "does", "cant", "wont",
    "dont", "didnt", "yeah", "okay", "haha", "lmao", "hmm", "here", "know",
})
