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
    billing: dict[str, int | str]

    @field_validator("model", "datatap", "capability_pack", "limits", "billing")
    @classmethod
    def copy_containers(cls, value):
        return dict(value)

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
