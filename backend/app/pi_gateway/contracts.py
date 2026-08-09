"""Strict, secret-free DTOs for the internal Pi Gateway protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


_SOURCE_EVENT_TYPES = {
    "agent.turn.start",
    "agent.turn.end",
    "agent/turn/start",
    "agent/turn/end",
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
}
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
        if self.event_type in {"message.delta", "text.delta", "thinking.delta"}:
            for key in ("delta", "text"):
                value = self.payload.get(key)
                if isinstance(value, str) and len(value) > 16_384:
                    raise ValueError("source_event_delta_too_large")
        return self


class PiGatewayClaimRequest(_StrictModel):
    capacity: int = Field(default=1, ge=1, le=128)


class PiGatewayHeartbeatRequest(_StrictModel):
    attempt_id: str = Field(min_length=1, max_length=64)


class PiGatewayTerminalRequest(_StrictModel):
    attempt_id: str = Field(min_length=1, max_length=64)
    outcome: Literal["completed", "completed_with_warnings", "failed", "cancelled"]
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def bound_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_payload(value, "terminal")
        return value


class PiGatewayClaimResponse(_StrictModel):
    run_id: str = Field(min_length=1, max_length=64)
    attempt_id: str = Field(min_length=1, max_length=64)
    lease_token: str = Field(min_length=32, max_length=512)
    runtime_snapshot: dict[str, Any]
    transcript: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    secret_envelope: RuntimeSecretEnvelope
    adapter_catalog: list[PiGatewayAdapterCatalogEntry] = Field(default_factory=list, max_length=32)
    internal_tools: list[dict[str, Any]] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def bounded_nested_response(self) -> "PiGatewayClaimResponse":
        if len(str(self.runtime_snapshot).encode("utf-8")) > 256 * 1024:
            raise ValueError("pi_gateway_claim_snapshot_too_large")
        if _contains_sensitive_key(self.runtime_snapshot, _SENSITIVE_PAYLOAD_KEYS):
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


def _validate_payload(value: dict[str, Any], prefix: str) -> None:
    if len(str(value).encode("utf-8")) > 64 * 1024:
        raise ValueError(f"{prefix}_payload_too_large")
    if _contains_sensitive_key(value, _SENSITIVE_PAYLOAD_KEYS):
        raise ValueError(f"{prefix}_payload_sensitive_field")
    if _contains_sensitive_key(value, _IDENTITY_PAYLOAD_KEYS):
        raise ValueError(f"{prefix}_payload_identity_field")
