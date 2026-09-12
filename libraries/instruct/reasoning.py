import re

# 🌸 Strips reasoning-model "thinking" leakage out of a reply before it's
# saved to memory or sent to Discord. Even though get_ai_response asks Groq
# for reasoning_format="hidden", some reasoning models (qwen3 especially)
# can still leak a <think>...</think> block into message.content — this is
# the belt-and-suspenders cleanup so Discord never sees raw chain-of-thought.
THINK_BLOCK_PATTERN = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
THINK_TAG_PATTERN   = re.compile(r"</?think>", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    if not text:
        return text
    text = THINK_BLOCK_PATTERN.sub("", text)
    text = THINK_TAG_PATTERN.sub("", text)
    return text.strip()
