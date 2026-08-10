"""安全的 Pi Gateway source-event 归一化。

Usage 事件是内部账务输入，不是用户可见的 AgentEvent。该模块只保留
token/cost 计算所需的有限字段，并把身份、凭证和原始供应商 payload 拦在
Gateway 边界之外。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .accounting import RuntimeUsageError


class PiGatewayEventError(ValueError):
    """Stable, secret-free source event boundary error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_EVENT_ALIASES = {
    "agent.turn.start": "run.started",
    "agent/turn/start": "run.started",
    "agent.turn.end": "turn.completed",
    "agent/turn/end": "turn.completed",
    "turn.start": "turn.started",
    "turn/start": "turn.started",
    "message.start": "message.started",
    "message/start": "message.started",
    "message.delta": "message.delta",
    "message/delta": "message.delta",
    "message.end": "message.completed",
    "message/end": "message.completed",
    "text.delta": "message.delta",
    "text/delta": "message.delta",
    "thinking.start": "thinking.started",
    "thinking/start": "thinking.started",
    "thinking.delta": "thinking.delta",
    "thinking/delta": "thinking.delta",
    "thinking.end": "thinking.completed",
    "thinking/end": "thinking.completed",
    "tool.start": "tool.started",
    "tool/start": "tool.started",
    "tool_call.start": "tool.started",
    "tool_call/start": "tool.started",
    "tool_call.end": "tool.completed",
    "tool_call/end": "tool.completed",
    "tool.end": "tool.completed",
    "tool/end": "tool.completed",
}

_SOURCE_EVENT_ALLOWED_FIELDS = {
    "run.started": set(),
    "turn.started": set(),
    "turn.completed": {"safe_summary"},
    "message.started": {"message_id", "role"},
    "message.delta": {"message_id", "delta", "text"},
    "message.completed": {"message_id", "text", "type"},
    "thinking.started": {"attempt"},
    "thinking.delta": {"attempt", "delta", "text"},
    "thinking.completed": {"attempt", "duration_ms"},
    "tool.started": {"call_id", "internal_tool_name", "safe_summary"},
    "tool.completed": {
        "call_id", "internal_tool_name", "status", "safe_summary", "duration_ms", "points", "error_code"
    },
}


def parse_source_event_id(source_event_id: str) -> tuple[str, int]:
    """Parse the immutable ``{attempt_id}:{worker_sequence}`` identity."""

    if not isinstance(source_event_id, str) or source_event_id.count(":") != 1:
        raise PiGatewayEventError("pi_gateway_source_event_invalid")
    attempt_id, raw_sequence = source_event_id.split(":", 1)
    if not attempt_id or not raw_sequence.isdigit():
        raise PiGatewayEventError("pi_gateway_source_event_invalid")
    sequence = int(raw_sequence)
    if sequence < 1 or sequence > 10_000_000:
        raise PiGatewayEventError("pi_gateway_source_sequence_invalid")
    return attempt_id, sequence


def canonical_event_type(event_type: str, payload: Mapping[str, Any] | None = None) -> str:
    """Map SDK aliases to the small product event vocabulary."""

    canonical = _EVENT_ALIASES.get(event_type, event_type)
    if canonical == "tool.completed":
        status = str((payload or {}).get("status", "")).lower()
        if status in {"unknown", "result_unknown"}:
            return "tool.unknown"
        if status in {"failed", "error", "failure"}:
            return "tool.failed"
        return "tool.succeeded"
    return canonical


def normalize_source_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project an SDK payload to fields safe for AgentEvent/SSE."""

    if not isinstance(payload, Mapping):
        raise PiGatewayEventError("pi_gateway_event_payload_invalid")
    canonical = canonical_event_type(event_type, payload)
    # 白名单按别名归一后的粗粒度类型（tool.completed）索引；状态细分
    # （tool.succeeded/failed/unknown）共享同一张字段白名单。
    alias_canonical = _EVENT_ALIASES.get(event_type, event_type)
    allowed = _SOURCE_EVENT_ALLOWED_FIELDS.get(canonical)
    if allowed is None:
        allowed = _SOURCE_EVENT_ALLOWED_FIELDS.get(alias_canonical)
    if allowed is None:
        raise PiGatewayEventError("pi_gateway_source_event_unknown")
    unknown = set(payload) - allowed
    if unknown:
        raise PiGatewayEventError("pi_gateway_event_field_invalid")
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"delta", "text", "safe_summary"}:
            if not isinstance(value, str) or len(value) > 64 * 1024:
                raise PiGatewayEventError("pi_gateway_event_text_invalid")
            result[key] = value
        elif key in {"message_id", "call_id", "internal_tool_name", "error_code", "role", "type"}:
            if not isinstance(value, str) or not value or len(value) > 128:
                raise PiGatewayEventError("pi_gateway_event_field_invalid")
            result[key] = value
        elif key in {"attempt", "duration_ms", "points"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10**9:
                raise PiGatewayEventError("pi_gateway_event_number_invalid")
            result[key] = value
        elif key == "status":
            if value not in {"succeeded", "failed", "unknown", "error", "result_unknown"}:
                raise PiGatewayEventError("pi_gateway_event_status_invalid")
            result[key] = value
    if canonical in {"message.delta", "thinking.delta"}:
        text = result.get("text", result.get("delta"))
        if not isinstance(text, str) or not text:
            raise PiGatewayEventError("pi_gateway_event_text_missing")
    return result

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


__all__ = [
    "PiGatewayEventError",
    "canonical_event_type",
    "normalize_source_payload",
    "normalize_usage_payload",
    "parse_source_event_id",
]
