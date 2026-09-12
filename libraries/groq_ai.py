"""🌸 groq_ai.py — FACADE ONLY.

This module used to be one big file holding GroqService end-to-end.
It's now split into the `engine/` package (see engine/service.py's
docstring for the module map). This file exists purely so every
existing caller/test that does `from groq_ai import GroqService` (or
patches `groq_ai.GroqService` / methods on that class) keeps working
with ZERO changes needed on their end.
"""

from engine.service import GroqService
from engine.dm_notice import _first_notice_line
from engine.logging import (
    groq_logger,
    groq_ai_logger,
    _groq_ai_relay_logger,
    SUCCESS_THUMBNAIL_URL,
    FAIL_THUMBNAIL_URL,
)

__all__ = [
    "GroqService",
    "_first_notice_line",
    "groq_logger",
    "groq_ai_logger",
    "_groq_ai_relay_logger",
    "SUCCESS_THUMBNAIL_URL",
    "FAIL_THUMBNAIL_URL",
]
