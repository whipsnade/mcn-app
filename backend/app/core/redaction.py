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
_URI_PATTERN = re.compile(
    r"(?:(?:https?|ftp|file|ws|wss):/{2,}|//)[^\s<>\"'，；。！？、)\]}]+",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[^\s,;，；]+", re.IGNORECASE)
_SECRET_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]*", re.IGNORECASE)
_JWT_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<key_quote>[\"']?)"
    r"(?:authorization|api[_\s-]?key|apikey|key|token|secret|credential|"
    r"password|passwd|endpoint|base[_\s-]?url|url)\b"
    r"(?P=key_quote)"
    r"\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;，；]+)",
    re.IGNORECASE,
)


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

    Keys containing credentials are replaced wholesale. Free-form credentials,
    service URLs and phone numbers are masked even when embedded in ordinary
    text. Unknown scalar values are returned unchanged.
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
        redacted = _BEARER_PATTERN.sub(_REDACTED, value)
        redacted = _SENSITIVE_ASSIGNMENT_PATTERN.sub(_REDACTED, redacted)
        redacted = _URI_PATTERN.sub(_REDACTED, redacted)
        redacted = _SECRET_TOKEN_PATTERN.sub(_REDACTED, redacted)
        redacted = _JWT_PATTERN.sub(_REDACTED, redacted)
        return _PHONE_PATTERN.sub(_REDACTED, redacted)
    return value


__all__ = ["redact_for_log"]
