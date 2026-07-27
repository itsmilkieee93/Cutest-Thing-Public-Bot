"""
🌸 resources/env_loader.py
Small shared helpers for reading credentials from environment variables
(populated from .env via python-dotenv) instead of flat files in auth/.

Put this file at resources/env_loader.py in your bot project.
Requires: pip install python-dotenv

All the rotation / multi-key / multi-account logic in each cog is kept
exactly as it was — only the *source* of the secrets changes (env vars
instead of auth/<file>).
"""

import os
from dotenv import load_dotenv

# Load .env once, as early as possible. Safe to import this module from
# many cogs — load_dotenv() is a no-op after the first successful call
# unless override=True is passed.
load_dotenv()


def env_str(name: str, default: str = "") -> str:
    """Read a single string value from the environment."""
    val = os.environ.get(name, default)
    return val.strip() if val else default


def env_list(name: str) -> list[str]:
    """
    Read a comma-separated list of values from the environment.
    Mirrors the old 'one key per line' file format — used for Groq /
    OpenRouter key pools that rotate on rate-limit errors.

    Example .env line:
        GROQ_API_KEYS=key_one,key_two,key_three
    """
    raw = os.environ.get(name, "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def env_cloudflare_account(index: int) -> tuple[str, str, list[str]]:
    """
    Read one Cloudflare AI Gateway account (1-indexed to match the old
    cloudflare_ai / cloudflare_ai_2 / cloudflare_ai_3 file numbering).

    Expects three env vars per account:
        CLOUDFLARE_ACCOUNT_ID_<n>
        CLOUDFLARE_GATEWAY_ID_<n>
        CLOUDFLARE_API_TOKENS_<n>   (comma-separated, supports multiple tokens)

    Returns (account_id, gateway_id, [token, ...]) — same shape _load_auth()
    used to return from the old flat files.
    """
    suffix = "" if index == 1 else f"_{index}"
    account_id = env_str(f"CLOUDFLARE_ACCOUNT_ID{suffix}")
    gateway_id = env_str(f"CLOUDFLARE_GATEWAY_ID{suffix}")
    tokens = env_list(f"CLOUDFLARE_API_TOKENS{suffix}")
    return account_id, gateway_id, tokens
