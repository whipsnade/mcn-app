"""Strict, secret-free DTOs for the internal Pi Gateway protocol."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from .catalog import (
    PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES,
    PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES,
    canonical_adapter_catalog_bytes,
    normalized_adapter_service,
)
from .events import normalize_source_payload, normalize_usage_payload


PI_GATEWAY_EVENT_BATCH_MAX_EVENTS = 32
PI_GATEWAY_EVENT_BATCH_MAX_BYTES = 128 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RuntimeSecretEnvelope(_StrictModel):
    """Encrypted per-Run secret payload; plaintext is never a DTO field."""

    alg: Literal["AES-256-GCM"]
    nonce: str = Field(min_length=16, max_length=64)
    ciphertext: str = Field(min_length=16, max_length=200_000)


class PiGatewayAdapterCatalogEntry(_StrictModel):
    catalog_entry_id: str = Field(min_length=1, max_length=64)
    adapter_visible_name: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=64)
    remote_name: str = Field(min_length=1, max_length=128)
    input_schema_digest: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")


class PiGatewayInternalToolRequest(_StrictModel):
    tool_name: str = Field(min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def bound_args(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 64:
            raise ValueError("args_too_many")
        return value


class PiGatewayMcpPreflightRequest(_StrictModel):
    """Adapter-visible call identity; price and catalog id are server-owned."""

    tool_name: str = Field(min_length=1, max_length=128)
    server: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def bound_args(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 64:
            raise ValueError("args_too_many")
        _validate_payload(value, "mcp_preflight")
        return value


class PiGatewayMcpPermitResponse(_StrictModel):
    permit_id: str = Field(min_length=1, max_length=64)
    catalog_entry_id: str = Field(min_length=1, max_length=64)
    amount: Literal[10] = 10


class PiGatewayMcpFinalizeRequest(_StrictModel):
    permit_id: str = Field(min_length=1, max_length=64)
    outcome: Literal["succeeded"]
    upstream_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    response_bytes: int | None = Field(default=None, ge=0, le=64 * 1024 * 1024)
    adapter_version: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    completed_at: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9:+.TZ_-]{1,64}$")
    response_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-fA-F]{64}$")


class PiGatewayMcpFailureMetadata(_StrictModel):
    version: Literal["mcp_failure_v1"]
    source: Literal[
        "call_failed",
        "aborted",
        "worker_rpc_timeout",
        "worker_rpc_disconnected",
        "finalize_ack_unknown",
        "other",
    ]
    # 提交 3：仅 metadata-only 可观测性，不改变分类语义（result_unknown 仍保持
    # 预留、不自动释放、不自动重放）。真实 round 的 14 个 unknown 均为 adapter
    # call_failed；这些字段让审计能区分「adapter 明确失败但结果未知」与「协议/
    # 阶段不明」。
    error_class: str | None = Field(default=None, max_length=64)
    received_jsonrpc_response: bool | None = None
    dispatch_phase: Literal["preflight", "dispatched", "unknown"] | None = None
    is_standard_mcp_error: bool | None = None
    upstream_request_id: str | None = Field(default=None, max_length=128)


class PiGatewayMcpFailRequest(_StrictModel):
    permit_id: str = Field(min_length=1, max_length=64)
    classification: Literal["definitely_not_sent", "failed_confirmed", "result_unknown"]
    metadata: PiGatewayMcpFailureMetadata | None = None


class PiGatewayProviderFailureMetadata(_StrictModel):
    """Metadata-only provider failure projection; raw SDK errors never cross this boundary."""

    version: Literal["provider_failure_v1"]
    failure_class: Literal[
        "authentication",
        "authorization",
        "rate_limited",
        "model_not_found",
        "invalid_request",
        "context_length",
        "timeout",
        "network",
        "upstream_5xx",
        "aborted",
        "unknown",
    ]
    http_status: StrictInt | None = Field(default=None, ge=100, le=599)
    provider_request_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,127})$",
    )
    error_fingerprint: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")
    observed_at: StrictStr | None = Field(
        default=None,
        min_length=24,
        max_length=24,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
    )

    @field_validator("provider_request_id")
    @classmethod
    def safe_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lower = value.lower()
        if lower.startswith(("bearer", "token", "secret", "api_key", "apikey", "sk-", "sk_")):
            raise ValueError("provider_request_id_sensitive")
        return value

    @field_validator("observed_at")
    @classmethod
    def safe_timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError as exc:
            raise ValueError("observed_at_invalid") from exc
        return value

    @model_validator(mode="after")
    def optional_fields_must_be_omitted(self) -> "PiGatewayProviderFailureMetadata":
        for field_name in ("http_status", "provider_request_id", "observed_at"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError("provider_failure_optional_field_null")
        return self

_SOURCE_EVENT_TYPES = {
    "agent.turn.start",
    "agent.turn.end",
    "agent/turn/start",
    "agent/turn/end",
    "turn.start",
    "turn/start",
    "message.start",
    "message.delta",
    "message.end",
    "message.completed",
    "message/start",
    "message/delta",
    "message/end",
    "tool.start",
    "tool.end",
    "tool_call.start",
    "tool_call.end",
    "tool_call/start",
    "tool_call/end",
    "tool/start",
    "tool/end",
    "thinking.start",
    "thinking.delta",
    "thinking.end",
    "thinking/start",
    "thinking/delta",
    "thinking/end",
    "text.delta",
    "text/delta",
    "usage",
}

_SENSITIVE_PAYLOAD_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "environment",
    "error_message",
    "errormessage",
    "raw_error",
    "response_body",
    "request_body",
    "prompt",
    "tool_arguments",
    "model_output",
}
# ``environment`` is a server-owned field of the immutable Runtime Snapshot.
# It remains forbidden in model/source-event payloads below, but must be
# accepted in the authenticated claim snapshot so non-production runtimes can
# be selected deterministically without making the claim impossible to decode.
_RUNTIME_SNAPSHOT_SENSITIVE_PAYLOAD_KEYS = _SENSITIVE_PAYLOAD_KEYS - {"environment"}
_IDENTITY_PAYLOAD_KEYS = {
    "tenant_id",
    "user_id",
    "session_id",
    "run_id",
    "attempt_id",
    "gateway_id",
    "lease_token",
}


class PiGatewaySourceEvent(_StrictModel):
    source_event_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,160}$")
    sequence: int = Field(ge=1, le=10_000_000)
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def known_event_type(cls, value: str) -> str:
        if value not in _SOURCE_EVENT_TYPES:
            raise ValueError("source_event_type_unknown")
        return value

    @model_validator(mode="after")
    def sequence_matches_source_id(self) -> "PiGatewaySourceEvent":
        try:
            if int(self.source_event_id.rsplit(":", 1)[1]) != self.sequence:
                raise ValueError("source_event_sequence_mismatch")
        except (IndexError, ValueError) as exc:
            raise ValueError("source_event_sequence_mismatch") from exc
        return self

    @field_validator("payload")
    @classmethod
    def bound_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_payload(value, "source_event")
        return value

    @model_validator(mode="after")
    def bound_delta(self) -> "PiGatewaySourceEvent":
        if self.event_type == "usage":
            self.payload = normalize_usage_payload(self.payload)
            return self
        self.payload = normalize_source_payload(self.event_type, self.payload)
        if self.event_type in {"message.delta", "text.delta", "thinking.delta"}:
            for key in ("delta", "text"):
                value = self.payload.get(key)
                if isinstance(value, str) and len(value) > 16_384:
                    raise ValueError("source_event_delta_too_large")
        return self


class PiGatewaySourceEventBatch(_StrictModel):
    events: list[PiGatewaySourceEvent] = Field(
        min_length=1,
        max_length=PI_GATEWAY_EVENT_BATCH_MAX_EVENTS,
    )

    @model_validator(mode="after")
    def bounded_contiguous_batch(self) -> "PiGatewaySourceEventBatch":
        attempts = {event.source_event_id.rsplit(":", 1)[0] for event in self.events}
        if len(attempts) != 1:
            raise ValueError("pi_gateway_event_batch_attempt_mismatch")
        if any(
            self.events[index].sequence != self.events[index - 1].sequence + 1
            for index in range(1, len(self.events))
        ):
            raise ValueError("pi_gateway_event_batch_sequence_gap")
        serialized = json.dumps(
            {"events": [event.model_dump(mode="json") for event in self.events]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(serialized) > PI_GATEWAY_EVENT_BATCH_MAX_BYTES:
            raise ValueError("pi_gateway_event_batch_too_large")
        return self


class PiGatewayClaimRequest(_StrictModel):
    capacity: int = Field(default=1, ge=1, le=128)


class PiGatewayHeartbeatRequest(_StrictModel):
    attempt_id: str = Field(min_length=1, max_length=64)


class PiGatewayTerminalRequest(_StrictModel):
    attempt_id: str = Field(min_length=1, max_length=64)
    outcome: Literal["completed", "completed_with_warnings", "failed", "cancelled"]
    payload: dict[str, Any] = Field(default_factory=dict)
    failure_metadata: PiGatewayProviderFailureMetadata | None = None

    @field_validator("payload")
    @classmethod
    def bound_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_payload(value, "terminal")
        return value

    @model_validator(mode="after")
    def provider_failure_metadata_boundary(self) -> "PiGatewayTerminalRequest":
        if self.failure_metadata is not None and self.outcome != "failed":
            raise ValueError("terminal_provider_failure_metadata_outcome_invalid")
        if self.failure_metadata is not None:
            business_code = self.payload.get("error_code") or self.payload.get("code")
            if business_code != "pi_model_provider_error":
                raise ValueError("terminal_provider_failure_metadata_code_invalid")
        if "failure_metadata" in self.payload:
            raise ValueError("terminal_provider_failure_metadata_must_be_top_level")
        return self


class PiGatewayClaimResponse(_StrictModel):
    @model_validator(mode="before")
    @classmethod
    def bound_adapter_catalog_payload(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        adapter_catalog = value.get("adapter_catalog")
        if not isinstance(adapter_catalog, list):
            return value
        if len(adapter_catalog) > PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES:
            raise ValueError("pi_gateway_claim_catalog_too_large")
        try:
            canonical_entries = [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in adapter_catalog
            ]
            catalog_bytes = canonical_adapter_catalog_bytes(canonical_entries)
        except (TypeError, ValueError) as exc:
            raise ValueError("pi_gateway_claim_catalog_invalid") from exc
        if len(catalog_bytes) > PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES:
            raise ValueError("pi_gateway_claim_catalog_too_large")
        return value

    run_id: str = Field(min_length=1, max_length=64)
    attempt_id: str = Field(min_length=1, max_length=64)
    lease_token: str = Field(min_length=32, max_length=512)
    # 明确的 lease deadline（epoch 秒）：Gateway 的 heartbeat 串行节奏、
    # 超时与失败重试预算都以它为界。
    lease_expires_at: float
    runtime_snapshot: dict[str, Any]
    transcript: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    secret_envelope: RuntimeSecretEnvelope
    adapter_catalog: list[PiGatewayAdapterCatalogEntry] = Field(
        default_factory=list,
        max_length=PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES,
    )
    internal_tools: list[dict[str, Any]] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def bounded_nested_response(self) -> "PiGatewayClaimResponse":
        if len(str(self.runtime_snapshot).encode("utf-8")) > 256 * 1024:
            raise ValueError("pi_gateway_claim_snapshot_too_large")
        if _contains_sensitive_key(self.runtime_snapshot, _RUNTIME_SNAPSHOT_SENSITIVE_PAYLOAD_KEYS):
            raise ValueError("pi_gateway_claim_snapshot_sensitive_field")
        for item in self.transcript:
            if (
                set(item) != {"role", "content"}
                or item.get("role") not in {"user", "assistant"}
                or not isinstance(item.get("content"), str)
                or len(item["content"]) > 32_000
                or _contains_sensitive_key(item, _SENSITIVE_PAYLOAD_KEYS)
            ):
                raise ValueError("pi_gateway_transcript_invalid")
        for item in self.internal_tools:
            if set(item) != {"name"} or not isinstance(item.get("name"), str) or not item["name"]:
                raise ValueError("pi_gateway_internal_tools_invalid")
        seen: set[str] = set()
        for item in self.adapter_catalog:
            identity = f"{normalized_adapter_service(item.service)}\u0000{item.adapter_visible_name}"
            if identity in seen:
                raise ValueError("pi_gateway_adapter_catalog_duplicate")
            seen.add(identity)
        return self


def _contains_sensitive_key(value: object, sensitive: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in sensitive or _contains_sensitive_key(item, sensitive)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item, sensitive) for item in value)
    return False


_PAYLOAD_MAX_BYTES = 64 * 1024


def _validate_payload(value: dict[str, Any], prefix: str) -> None:
    if len(str(value).encode("utf-8")) > _PAYLOAD_MAX_BYTES:
        raise ValueError(f"{prefix}_payload_too_large")
    if _contains_sensitive_key(value, _SENSITIVE_PAYLOAD_KEYS):
        raise ValueError(f"{prefix}_payload_sensitive_field")
    if _contains_sensitive_key(value, _IDENTITY_PAYLOAD_KEYS):
        raise ValueError(f"{prefix}_payload_identity_field")
