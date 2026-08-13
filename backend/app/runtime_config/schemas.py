"""Public, secret-free runtime configuration contracts."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator


class FrozenDict(dict[str, Any]):
    """JSON-shaped mapping that cannot be mutated after snapshot creation."""

    __slots__ = ()

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        raise TypeError("runtime_snapshot_is_immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(item) for item in value)
    return value


class RuntimeConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    config_version_id: str
    runtime_contract_version: Literal["marketing_runtime_v1"]
    runtime_backend: Literal["current", "pi"]
    model: dict[str, str | int | float | None]
    datatap: dict[str, object]
    capability_pack: dict[str, object]
    # Legacy snapshots may omit these fields.  Every new Run snapshot produced
    # by RuntimeConfigService fills them from the reviewed pack/profile policy.
    profile_name: str | None = None
    # Explicitly distinguishes a profile that produces no required artifact
    # from a snapshot that accidentally lost its contract mapping.
    artifact_contract_mode: Literal["required", "none"] = "none"
    required_artifact_contract: str | None = None
    capability_pack_version: str | None = None
    capability_pack_manifest_digest: str | None = None
    limits: dict[str, int | float]
    # ``price_table`` is a nested, public snapshot contract.  Secrets remain
    # forbidden recursively; values are integer micros (or bounded labels).
    billing: dict[str, int | str | dict[str, int | str]]
    # Reviewed adapter bindings are captured together with the Run snapshot;
    # claim/terminal/recovery may read them but must never append to the row.
    adapter_catalog: tuple[dict[str, object], ...] = ()

    @field_validator("model", "datatap", "capability_pack", "limits", "billing")
    @classmethod
    def copy_containers(cls, value):
        return _deep_freeze(dict(value))

    @field_validator("adapter_catalog")
    @classmethod
    def freeze_adapter_catalog(cls, value):
        return tuple(_deep_freeze(dict(entry)) for entry in value)

    @field_validator("billing")
    @classmethod
    def validate_billing(cls, value: dict[str, int | str | dict[str, int | str]]):
        if set(value) - {"mcp_call_points", "price_table"}:
            raise ValueError("runtime_billing_config_invalid")
        table = value.get("price_table")
        if table is not None:
            if not isinstance(table, dict) or not table:
                raise ValueError("runtime_price_table_invalid")
            allowed = {
                "version",
                "currency",
                "input_micros_per_million",
                "output_micros_per_million",
                "cache_read_micros_per_million",
                "cache_write_micros_per_million",
                "input_micros_per_token",
                "output_micros_per_token",
                "cache_read_micros_per_token",
                "cache_write_micros_per_token",
            }
            if set(table) - allowed:
                raise ValueError("runtime_price_table_invalid")
            for key, item in table.items():
                if key in {"version", "currency"}:
                    max_length = 8 if key == "currency" else 32
                    if not isinstance(item, str) or not item or len(item) > max_length:
                        raise ValueError("runtime_price_table_invalid")
                elif isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 10**12:
                    raise ValueError("runtime_price_table_invalid")
        return value

    @model_validator(mode="after")
    def reject_secret_fields(self) -> RuntimeConfigSnapshot:
        forbidden = {"api_key", "token", "password", "secret", "authorization", "key"}
        for container in (
            self.model,
            self.datatap,
            self.adapter_catalog,
            self.capability_pack,
            self.limits,
            self.billing,
        ):
            if _contains_forbidden_key(container, forbidden) or _contains_credential_value(container):
                raise ValueError("runtime_snapshot_secret_field")
        nested_pack_version = self.capability_pack.get("pack_version")
        if (
            self.capability_pack_version is not None
            and isinstance(nested_pack_version, str)
            and self.capability_pack_version != nested_pack_version
        ):
            raise ValueError("runtime_snapshot_capability_audit_mismatch")
        nested_manifest_digest = self.capability_pack.get("manifest_digest")
        if (
            self.capability_pack_manifest_digest is not None
            and isinstance(nested_manifest_digest, str)
            and self.capability_pack_manifest_digest != nested_manifest_digest
        ):
            raise ValueError("runtime_snapshot_capability_audit_mismatch")
        if self.required_artifact_contract is not None and not self.profile_name:
            raise ValueError("runtime_snapshot_profile_missing")
        if self.artifact_contract_mode == "required":
            if not self.profile_name or not self.required_artifact_contract:
                raise ValueError("runtime_snapshot_artifact_contract_missing")
        elif self.required_artifact_contract is not None:
            raise ValueError("runtime_snapshot_artifact_contract_mode_invalid")
        return self


class RuntimeSecretBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    model_base_url: SecretStr
    model_api_key: SecretStr
    datatap_token: SecretStr
    datatap_urls: dict[str, SecretStr]

    @field_validator("datatap_urls")
    @classmethod
    def copy_urls(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        return dict(value)


def _contains_forbidden_key(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in forbidden or _contains_forbidden_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_forbidden_key(item, forbidden) for item in value)
    return False


_CREDENTIAL_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9][A-Za-z0-9._-]+|Bearer\s+\S+|[?&](?:token|api_key|key)=[^&#\s]+|://[^/\s:@]+:[^@/\s]+@)",
    re.IGNORECASE,
)


def _contains_credential_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(_CREDENTIAL_PATTERN.search(value))
    if isinstance(value, Mapping):
        return any(_contains_credential_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_credential_value(item) for item in value)
    return False
