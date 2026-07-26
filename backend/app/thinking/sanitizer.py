from __future__ import annotations

import re
from dataclasses import dataclass


_HIDDEN = "[已隐藏]"
_SCHEMA_HIDDEN = "[输出结构说明已隐藏]"
_TRUNCATED_SUFFIX = "思考内容过长，已截断"

_BEARER_RE = re.compile(r"\bBearer\s+[^\s,;，；\"']+", re.IGNORECASE)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"
    r"(?![A-Za-z0-9_-])"
)
_API_KEY_RE = re.compile(
    r"\b(api[_ -]?key|access[_ -]?token|secret[_ -]?key|token)"
    r"(\s*[:=]\s*)[^\s,;，；\"']+",
    re.IGNORECASE,
)
_SYSTEM_TAG_RE = re.compile(r"<system\b[^>]*>.*?</system\s*>", re.IGNORECASE | re.DOTALL)
_SYSTEM_SEGMENT_RE = re.compile(
    r"(^|\n)"
    r"(?:#{1,6}\s*)?"
    r"(?:系统提示词|系统提示|system[ _-]?prompt)"
    r"\s*[:：]\s*"
    r".*?"
    r"(?="
    r"\n(?:#{1,6}\s*)?"
    r"(?:用户消息|用户提示|用户输入|user[ _-]?(?:prompt|message)|JSON\s+Schema|输出结构)"
    r"\s*[:：]"
    r"|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_SCHEMA_HEADER_RE = re.compile(
    r"(?:#{1,6}\s*)?"
    r"(?:JSON\s+Schema|输出\s*(?:JSON\s*)?(?:Schema|结构(?:说明)?))"
    r"\s*[:：]\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SanitizedThinking:
    text: str
    truncated: bool


def _hide_secrets(text: str) -> str:
    hidden = _BEARER_RE.sub(_HIDDEN, text)
    hidden = _JWT_RE.sub(_HIDDEN, hidden)
    return _API_KEY_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{_HIDDEN}", hidden)


def _hide_system_prompts(text: str) -> str:
    hidden = _SYSTEM_TAG_RE.sub(_HIDDEN, text)
    return _SYSTEM_SEGMENT_RE.sub(lambda match: f"{match.group(1)}{_HIDDEN}\n", hidden)


def _balanced_json_end(text: str, start: int) -> int | None:
    opening = text[start]
    if opening not in "[{":
        return None
    stack = [opening]
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack or stack[-1] != pairs[char]:
                return None
            stack.pop()
            if not stack:
                return index + 1
    return None


def _hide_json_schemas(text: str) -> str:
    search_from = 0
    while match := _SCHEMA_HEADER_RE.search(text, search_from):
        json_start = next(
            (
                index
                for index in range(match.end(), len(text))
                if text[index] in "[{"
            ),
            None,
        )
        if json_start is None:
            text = f"{text[: match.start()]}{_SCHEMA_HIDDEN}"
            break
        json_end = _balanced_json_end(text, json_start)
        if json_end is None:
            text = f"{text[: match.start()]}{_SCHEMA_HIDDEN}"
            break
        while json_end < len(text) and text[json_end] in " \t":
            json_end += 1
        if text.startswith("```", json_end):
            json_end += 3
        text = f"{text[: match.start()]}{_SCHEMA_HIDDEN}{text[json_end:]}"
        search_from = match.start() + len(_SCHEMA_HIDDEN)
    return text


def _limit_length(text: str, max_chars: int) -> SanitizedThinking:
    if len(text) <= max_chars:
        return SanitizedThinking(text=text, truncated=False)
    suffix = _TRUNCATED_SUFFIX[:max_chars]
    return SanitizedThinking(
        text=f"{text[: max(0, max_chars - len(suffix))]}{suffix}",
        truncated=True,
    )


def sanitize_thinking(text: str, *, max_chars: int = 12_000) -> SanitizedThinking:
    """生成可公开展示的思考文本，不保留任何原始敏感片段。"""

    if max_chars < 0:
        raise ValueError("max_chars must not be negative")
    sanitized = _hide_secrets(text)
    sanitized = _hide_system_prompts(sanitized)
    sanitized = _hide_json_schemas(sanitized)
    return _limit_length(sanitized, max_chars)


__all__ = ["SanitizedThinking", "sanitize_thinking"]
