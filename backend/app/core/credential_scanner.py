"""Centralized detector for credentials that must never enter public snapshots."""

from __future__ import annotations

import re
from collections.abc import Mapping


_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-|AIza)[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"[?&](?:token|api[_-]?key|access[_-]?token|secret|password|key)=[^&#\s]+", re.IGNORECASE),
    re.compile(r"\b(?:mysql|postgres(?:ql)?|redis|mongodb)://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
)


def contains_credential(value: object) -> bool:
    """Recursively detect common private-key, token and credential URL shapes."""

    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS)
    if isinstance(value, Mapping):
        return any(contains_credential(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_credential(item) for item in value)
    return False


__all__ = ["contains_credential"]
