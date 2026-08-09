"""安全的 Pi Gateway source-event 归一化。

Usage 事件是内部账务输入，不是用户可见的 AgentEvent。该模块只保留
token/cost 计算所需的有限字段，并把身份、凭证和原始供应商 payload 拦在
Gateway 边界之外。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .accounting import RuntimeUsageError

_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "upstream_request_id",
    "request_id",
    "provider",
    "model",
    "usage_status",
}
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _safe_text(value: object, *, code: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise RuntimeUsageError(code)
    if any(ord(char) < 32 for char in value):
        raise RuntimeUsageError(code)
    return value


def normalize_usage_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a server-safe usage projection or raise a stable error code."""

    if not isinstance(payload, Mapping):
        raise RuntimeUsageError("runtime_usage_payload_invalid")
    unknown = set(payload) - _USAGE_KEYS
    if unknown:
        raise RuntimeUsageError("runtime_usage_payload_field_invalid")
    normalized: dict[str, Any] = {}
    for key in _TOKEN_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10**12:
            raise RuntimeUsageError("runtime_usage_value_invalid")
        normalized[key] = value
    request_id = payload.get("upstream_request_id", payload.get("request_id"))
    if request_id is not None:
        normalized["upstream_request_id"] = _safe_text(
            request_id, code="runtime_usage_request_id_invalid", max_length=128
        )
    for key, max_length in (("provider", 64), ("model", 128)):
        value = payload.get(key)
        if value is not None:
            normalized[key] = _safe_text(value, code="runtime_usage_metadata_invalid", max_length=max_length)
    status = payload.get("usage_status")
    if status is not None and status not in {"available", "unavailable"}:
        raise RuntimeUsageError("runtime_usage_status_invalid")
    normalized["usage_status"] = (
        "available" if any(key in normalized for key in _TOKEN_KEYS) else "unavailable"
    )
    return normalized


__all__ = ["normalize_usage_payload"]
