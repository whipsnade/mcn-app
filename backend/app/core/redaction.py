"""Utilities for keeping credentials and personal data out of logs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "set_cookie",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "token",
    "credential",
)
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:1[3-9]\d{9})(?!\d)")


def _normalise_key(key: object) -> str:
    if not isinstance(key, str):
        return ""
    # Handle camelCase and punctuation variants without exposing the original key.
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _sensitive_key(key: object) -> bool:
    normalised = _normalise_key(key)
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


def redact_for_log(value: Any) -> Any:
    """Return a log-safe copy of nested data.

    Keys containing credentials are replaced wholesale. Phone numbers are masked
    even when embedded in ordinary text, which protects structured and free-form
    log fields alike. Unknown scalar values are returned unchanged.
    """

    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _sensitive_key(key) else redact_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_for_log(item) for item in value)
    if isinstance(value, str):
        return _PHONE_PATTERN.sub(_REDACTED, value)
    return value


__all__ = ["redact_for_log"]
