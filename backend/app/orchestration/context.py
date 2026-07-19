from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any
import unicodedata

from app.orchestration.schemas import PlannerMessage


_OMIT = object()
_SENSITIVE_REPORT_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "endpoint",
    "host",
    "token",
    "url",
}
_SENSITIVE_REPORT_KEY_PARTS = {
    "api_key",
    "authorization",
    "credential",
    "endpoint",
    "host",
    "token",
    "url",
}
_DISABLED_SERVICE_NAMES = {
    "zhihu-mcp",
    "toutiao-mcp",
    "baidu-index-mcp",
    "google-trends-mcp",
}
_SUPPLIER_HOST_NAMES = {"datatap.deepminer.com.cn"}
_TEXT_SECRET_PATTERN = re.compile(
    r"(?<![a-z0-9_])(?:authorization|bearer|api[ _-]?key|token|credentials?)(?![a-z0-9_])",
    re.IGNORECASE,
)


def compress_messages(messages: Sequence[Any], *, max_chars: int) -> tuple[PlannerMessage, ...]:
    """保留最新消息，且不让任何消息绕过规划 Prompt 的长度边界。"""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    remaining = max_chars
    selected: list[PlannerMessage] = []
    for message in reversed(messages):
        if remaining <= 0:
            break
        content = str(message.content)
        if len(content) > remaining:
            content = content[-remaining:]
        if content:
            selected.append(
                PlannerMessage(
                    role=message.role,
                    content=content,
                    sequence=message.sequence,
                )
            )
            remaining -= len(content)
    return tuple(reversed(selected))


def _normalized_key(key: str) -> str:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")


def _is_sensitive_report_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_REPORT_KEYS or any(
        part in normalized for part in _SENSITIVE_REPORT_KEY_PARTS
    )


def _contains_text_secret(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    return _TEXT_SECRET_PATTERN.search(normalized) is not None


def _project_reporting_value(value: Any) -> Any:
    if isinstance(value, dict):
        projected = {
            key: child
            for key, item in value.items()
            if isinstance(key, str)
            and not _is_sensitive_report_key(key)
            and (child := _project_reporting_value(item)) is not _OMIT
        }
        return projected
    if isinstance(value, list):
        return [
            child
            for item in value
            if (child := _project_reporting_value(item)) is not _OMIT
        ]
    if isinstance(value, str):
        value_lower = value.casefold()
        if (
            any(service_name in value_lower for service_name in _DISABLED_SERVICE_NAMES)
            or any(host_name in value_lower for host_name in _SUPPLIER_HOST_NAMES)
            or "http://" in value_lower
            or "https://" in value_lower
            or _contains_text_secret(value)
        ):
            return _OMIT
    return value


def project_reporting_summary(summary: dict[str, Any]) -> dict[str, Any]:
    projected = _project_reporting_value(summary)
    if not isinstance(projected, dict):
        raise TypeError("reporting context summary must be an object")
    return projected
