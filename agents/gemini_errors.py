"""
Structured Gemini API error classification.

String-matches raw Agno/Gemini error content against known markers and
returns a typed error code + user-facing message.  User-facing messages
never reference "Gemini" — they describe the service impact instead.
"""

from __future__ import annotations

import enum
from typing import Any

# ── Error code enum ──────────────────────────────────────────────────────────


class GeminiErrorCode(enum.Enum):
    """Typed classification of a Gemini / Agno LLM call failure."""

    QUOTA_EXCEEDED = "quota_exceeded"          # 429 / RESOURCE_EXHAUSTED
    MODEL_OVERLOADED = "model_overloaded"       # 503 on model servers
    TRANSIENT = "transient"                     # 502 / 503 / 504 / UNAVAILABLE
    DEADLINE_EXCEEDED = "deadline_exceeded"     # timeout
    PERMISSION_DENIED = "permission_denied"     # 403 / invalid key
    MALFORMED_FUNCTION_CALL = "malformed_function_call"
    UNKNOWN = "unknown"


# ── User-facing messages — NEVER mention "Gemini" ────────────────────────────

_USER_MESSAGES: dict[GeminiErrorCode, str] = {
    GeminiErrorCode.QUOTA_EXCEEDED: (
        "Our services are currently unavailable. Please try again later."
    ),
    GeminiErrorCode.MODEL_OVERLOADED: (
        "Our services are experiencing high demand. Please try again in a few minutes."
    ),
    GeminiErrorCode.TRANSIENT: (
        "A temporary issue occurred. The system will retry automatically."
    ),
    GeminiErrorCode.DEADLINE_EXCEEDED: (
        "The analysis step took too long to respond. "
        "Please try again with a smaller dataset or fewer tables."
    ),
    GeminiErrorCode.PERMISSION_DENIED: (
        "API key configuration error. Please contact support."
    ),
    GeminiErrorCode.MALFORMED_FUNCTION_CALL: (
        "An internal processing error occurred. The system will retry automatically."
    ),
    GeminiErrorCode.UNKNOWN: (
        "An unexpected error occurred. Please try again or contact support."
    ),
}

# ── Markers used for classification ──────────────────────────────────────────

# Order matters: more-specific markers checked first (e.g. quota before transient).

_QUOTA_MARKERS = (
    "RESOURCE_EXHAUSTED", "429", "quota", "Quota exceeded",
    "rate limit", "Rate limit", "RATE_LIMIT",
)

_MODEL_OVERLOAD_MARKERS = (
    "503", "model is overloaded", "overloaded",
)

_TRANSIENT_MARKERS = (
    "502", "504",
    "Bad Gateway", "Service Unavailable",
    "UNAVAILABLE", "Server Error",
    "Error 502", "Error 503", "Error 504",
    "INTERNAL", "Internal Server Error",
)

_DEADLINE_MARKERS = (
    "DEADLINE_EXCEEDED", "timed out", "timeout", "Timeout",
    "deadline exceeded",
)

_PERMISSION_MARKERS = (
    "PERMISSION_DENIED", "API_KEY_INVALID",
    "API key not valid", "permission",
    "403", "forbidden", "Forbidden",
)

_MALFORMED_MARKERS = (
    "MALFORMED_FUNCTION_CALL", "malformed function call",
    "Invalid JSON", "JSON parsing error",
)


# ── Public API ───────────────────────────────────────────────────────────────


def classify_gemini_error(content: Any) -> tuple[GeminiErrorCode, str]:
    """Classify raw error content and return (code, user_message).

    Args:
        content: Raw error string from Agno agent output or exception message.

    Returns:
        (GeminiErrorCode, user_facing_message) — the message is safe to show
        directly to end users.
    """
    if not isinstance(content, str) or not content.strip():
        return GeminiErrorCode.UNKNOWN, _USER_MESSAGES[GeminiErrorCode.UNKNOWN]

    lower = content.lower()

    # Check in priority order — first match wins
    for marker in _QUOTA_MARKERS:
        if marker.lower() in lower:
            return GeminiErrorCode.QUOTA_EXCEEDED, _USER_MESSAGES[GeminiErrorCode.QUOTA_EXCEEDED]

    for marker in _MODEL_OVERLOAD_MARKERS:
        if marker.lower() in lower:
            return GeminiErrorCode.MODEL_OVERLOADED, _USER_MESSAGES[GeminiErrorCode.MODEL_OVERLOADED]

    for marker in _DEADLINE_MARKERS:
        if marker.lower() in lower:
            return GeminiErrorCode.DEADLINE_EXCEEDED, _USER_MESSAGES[GeminiErrorCode.DEADLINE_EXCEEDED]

    for marker in _PERMISSION_MARKERS:
        if marker.lower() in lower:
            return GeminiErrorCode.PERMISSION_DENIED, _USER_MESSAGES[GeminiErrorCode.PERMISSION_DENIED]

    for marker in _MALFORMED_MARKERS:
        if marker.lower() in lower:
            return GeminiErrorCode.MALFORMED_FUNCTION_CALL, _USER_MESSAGES[GeminiErrorCode.MALFORMED_FUNCTION_CALL]

    for marker in _TRANSIENT_MARKERS:
        if marker.lower() in lower:
            return GeminiErrorCode.TRANSIENT, _USER_MESSAGES[GeminiErrorCode.TRANSIENT]

    return GeminiErrorCode.UNKNOWN, _USER_MESSAGES[GeminiErrorCode.UNKNOWN]


def get_gemini_error_message(error_code: GeminiErrorCode) -> str:
    """Return the user-facing message for a given error code."""
    return _USER_MESSAGES.get(error_code, _USER_MESSAGES[GeminiErrorCode.UNKNOWN])


def is_quota_error(content: Any) -> bool:
    """Quick boolean check — True if the error is quota/rate-limit related."""
    code, _ = classify_gemini_error(content)
    return code == GeminiErrorCode.QUOTA_EXCEEDED


def is_transient_error(content: Any) -> bool:
    """True if the error is transient (quota, overload, network blip, timeout).

    Returns False for permission errors and unknown errors — those are permanent.
    """
    code, _ = classify_gemini_error(content)
    return code in (
        GeminiErrorCode.QUOTA_EXCEEDED,
        GeminiErrorCode.MODEL_OVERLOADED,
        GeminiErrorCode.TRANSIENT,
        GeminiErrorCode.DEADLINE_EXCEEDED,
        GeminiErrorCode.MALFORMED_FUNCTION_CALL,
    )


def gemini_error_detail_payload(
    error_code: GeminiErrorCode,
    step: str,
) -> dict:
    """Build a structured error detail dict for WebSocket / API responses."""
    return {
        "error_type": error_code.value,
        "error_step": step or "unknown",
        "user_message": get_gemini_error_message(error_code),
        "is_retryable": error_code
        not in (GeminiErrorCode.PERMISSION_DENIED, GeminiErrorCode.UNKNOWN),
    }
