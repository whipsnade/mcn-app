"""Public, secret-free runtime configuration contracts."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator


class RuntimeConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    config_version_id: str
    runtime_contract_version: Literal["marketing_runtime_v1"]
    runtime_backend: Literal["current", "pi"]
    model: dict[str, str | int | float | None]
    datatap: dict[str, object]
    capability_pack: dict[str, object]
    limits: dict[str, int | float]
    # ``price_table`` is a nested, public snapshot contract.  Secrets remain
    # forbidden recursively; values are integer micros (or bounded labels).
    billing: dict[str, int | str | dict[str, int | str]]

    @field_validator("model", "datatap", "capability_pack", "limits", "billing")
    @classmethod
    def copy_containers(cls, value):
        return dict(value)

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
        for container in (self.model, self.datatap, self.capability_pack, self.limits, self.billing):
            if _contains_forbidden_key(container, forbidden) or _contains_credential_value(container):
                raise ValueError("runtime_snapshot_secret_field")
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
