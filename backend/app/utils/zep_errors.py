"""Helpers for recognizing and normalizing Zep API failures."""

from __future__ import annotations

import re
from typing import Optional


def stringify_zep_error(exc: object) -> str:
    """Return a normalized string form for logging and matching."""
    return str(exc or "")


def is_zep_rate_limit_error(exc: object) -> bool:
    """Detect Zep free-plan / 429 rate-limit failures."""
    message = stringify_zep_error(exc).lower()
    return (
        "rate limit" in message or
        "status_code: 429" in message or
        "status code: 429" in message or
        "retry-after" in message or
        "free plan" in message
    )


def is_zep_usage_limit_error(exc: object) -> bool:
    """Detect hard account quota / usage-limit failures from Zep."""
    message = stringify_zep_error(exc).lower()
    return (
        "episode usage limit" in message or
        "over the episode usage limit" in message or
        ("status_code: 403" in message and "forbidden" in message and "usage limit" in message) or
        ("status code: 403" in message and "forbidden" in message and "usage limit" in message)
    )


def extract_retry_after_seconds(exc: object) -> Optional[int]:
    """Best-effort parse of Retry-After from exception text."""
    message = stringify_zep_error(exc)
    match = re.search(r"retry-after['\"]?\s*[:=]\s*['\"]?(\d+)", message, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def build_zep_rate_limit_message(retry_after: Optional[int] = None, using_cache: bool = False) -> str:
    """Human-friendly message for UI / logs."""
    base = "Zep FREE plan rate limit reached"
    if using_cache:
        base += ", using cached graph snapshot"
    if retry_after:
        base += f". Retry after about {retry_after}s."
    else:
        base += ". Please retry a bit later."
    return base


def build_zep_usage_limit_message(using_local_preview: bool = False) -> str:
    """Human-friendly message for exhausted Zep usage quota."""
    base = "Zep account episode usage limit reached"
    if using_local_preview:
        base += ", switched to a local ontology preview graph"
    else:
        base += ". Please increase quota or use another Zep API key."
    return base
