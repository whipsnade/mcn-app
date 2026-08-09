"""Tenant runtime configuration snapshots and per-Run secret resolution."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun
from app.core.config import get_settings
from app.marketing_capability_pack.runtime import (
    MarketingRunCapability,
    build_marketing_run_capability,
)
from app.tenancy.models import Tenant

from .crypto import EncryptedSecretValue, RuntimeConfigError, SecretCipher
from .models import EncryptedRuntimeSecret, RuntimeConfigVersion
from .schemas import RuntimeConfigSnapshot, RuntimeSecretBundle

LEGACY_RUNTIME_CONFIG_ID = "legacy-env-v1"
POC_RUNTIME_CONFIG_ID = "poc-isolated-v1"
RUNTIME_CONTRACT_VERSION = "marketing_runtime_v1"


class RuntimeConfigService:
    """Append-only config service used by Run creators and the Gateway boundary."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        cipher: SecretCipher | None = None,
        now_fn=None,
    ) -> None:
        self.db = db
        self._cipher = cipher
        self._now_fn = now_fn or _utc_now

    @property
    def cipher(self) -> SecretCipher:
        if self._cipher is None:
            settings = get_settings()
            self._cipher = SecretCipher.from_environment(
                settings.runtime_secret_master_keys.get_secret_value(),
                settings.runtime_secret_active_key_version,
            )
        return self._cipher

    async def create_tenant_version(
        self,
        tenant_id: str,
        *,
        created_by: str,
        runtime_backend: str,
        model: dict[str, Any],
        datatap: dict[str, Any],
        limits: dict[str, int | float],
        billing: dict[str, int | str],
        secrets: RuntimeSecretBundle | None = None,
        runtime_contract_version: str = RUNTIME_CONTRACT_VERSION,
    ) -> RuntimeConfigVersion:
        if runtime_backend not in {"current", "pi"}:
            raise RuntimeConfigError("runtime_backend_invalid")
        if runtime_contract_version != RUNTIME_CONTRACT_VERSION:
            # Keep the row for an explicit fail-closed compatibility test, but it
            # can never be selected as a valid Run snapshot.
            contract = runtime_contract_version
        else:
            contract = RUNTIME_CONTRACT_VERSION
        if runtime_backend == "pi" and secrets is None:
            raise RuntimeConfigError("runtime_secrets_required")
        tenant = await self.db.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise RuntimeConfigError("runtime_tenant_not_found")
        version = int(
            await self.db.scalar(
                select(func.coalesce(func.max(RuntimeConfigVersion.version), 0)).where(
                    RuntimeConfigVersion.scope == "tenant",
                    RuntimeConfigVersion.tenant_id == tenant_id,
                )
            )
            or 0
        ) + 1
        config_id = f"tenant-{tenant_id[:12]}-v{version}-{uuid4().hex[:8]}"
        config = RuntimeConfigVersion(
            id=config_id,
            scope="tenant",
            tenant_id=tenant_id,
            version=version,
            status="draft",
            runtime_backend=runtime_backend,
            runtime_contract_version=contract,
            config_json=self._config_json(
                config_id=config_id,
                runtime_contract_version=contract,
                runtime_backend=runtime_backend,
                model=model,
                datatap=datatap,
                limits=limits,
                billing=billing,
            ),
            secret_refs_json=[],
            created_by=created_by,
            created_at=self._now_fn(),
        )
        self.db.add(config)
        await self.db.flush()
        if secrets is not None:
            refs = await self._persist_secrets(tenant_id, config_id, secrets)
            config.secret_refs_json = refs
            await self.db.flush()
        return config

    async def activate(self, config_version_id: str) -> RuntimeConfigVersion:
        config = await self.db.scalar(
            select(RuntimeConfigVersion)
            .where(RuntimeConfigVersion.id == config_version_id)
            .with_for_update()
        )
        if config is None:
            raise RuntimeConfigError("runtime_config_not_found")
        if config.status == "retired":
            raise RuntimeConfigError("runtime_config_immutable")
        if (config.scope == "tenant") != (config.tenant_id is not None):
            raise RuntimeConfigError("runtime_config_scope_invalid")
        if config.runtime_backend == "pi" and not config.secret_refs_json:
            raise RuntimeConfigError("runtime_secrets_required")
        now = self._now_fn()
        if config.scope == "tenant":
            tenant = await self.db.scalar(
                select(Tenant).where(Tenant.id == config.tenant_id).with_for_update()
            )
            if tenant is None:
                raise RuntimeConfigError("runtime_tenant_not_found")
            previous = await self.db.scalars(
                select(RuntimeConfigVersion)
                .where(
                    RuntimeConfigVersion.scope == "tenant",
                    RuntimeConfigVersion.tenant_id == config.tenant_id,
                    RuntimeConfigVersion.status == "active",
                    RuntimeConfigVersion.id != config.id,
                )
                .with_for_update()
            )
            for row in previous:
                row.status = "retired"
            tenant.active_runtime_config_id = config.id
        else:
            previous = await self.db.scalars(
                select(RuntimeConfigVersion)
                .where(
                    RuntimeConfigVersion.scope == "system",
                    RuntimeConfigVersion.status == "active",
                    RuntimeConfigVersion.id != config.id,
                )
                .with_for_update()
            )
            for row in previous:
                row.status = "retired"
        config.status = "active"
        config.activated_at = now
        await self.db.flush()
        return config

    async def update_version(self, config_version_id: str, *, config_json: dict[str, Any]) -> None:
        del config_version_id, config_json
        raise RuntimeConfigError("runtime_config_immutable")

    async def snapshot_for_new_run(self, tenant_id: str) -> RuntimeConfigSnapshot:
        tenant = await self.db.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise RuntimeConfigError("runtime_tenant_not_found")
        config = None
        if tenant.active_runtime_config_id is not None:
            config = await self.db.get(RuntimeConfigVersion, tenant.active_runtime_config_id)
            if config is None or config.scope != "tenant" or config.tenant_id != tenant_id:
                raise RuntimeConfigError("runtime_config_tenant_mismatch")
        if config is None:
            config = await self.db.scalar(
                select(RuntimeConfigVersion)
                .where(
                    RuntimeConfigVersion.scope == "system",
                    RuntimeConfigVersion.status == "active",
                )
                .order_by(RuntimeConfigVersion.version.desc())
                .limit(1)
            )
        if config is None or config.status != "active":
            raise RuntimeConfigError("runtime_config_required")
        if tenant.runtime_backend == "pi" and (
            config.scope != "tenant" or config.runtime_backend != "pi"
        ):
            raise RuntimeConfigError("runtime_config_required")
        if config.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
            raise RuntimeConfigError("runtime_contract_unsupported")
        if config.runtime_backend == "pi" and not config.secret_refs_json:
            raise RuntimeConfigError("runtime_config_required")
        snapshot = self._snapshot_from_config(config)
        if snapshot.runtime_backend != tenant.runtime_backend and tenant.runtime_backend == "current":
            # A current tenant may use the legacy current fallback only.  An
            # explicit pi override must not silently switch the tenant back.
            raise RuntimeConfigError("runtime_config_backend_mismatch")
        return snapshot

    async def snapshot_for_existing_run(
        self, run: AgentRun, *, parent_run_id: str | None = None
    ) -> RuntimeConfigSnapshot:
        del parent_run_id
        if not run.runtime_config_snapshot_json or not run.runtime_config_version_id:
            raise RuntimeConfigError("runtime_snapshot_missing")
        snapshot_payload = copy.deepcopy(run.runtime_config_snapshot_json)
        adapter_catalog = snapshot_payload.pop("adapter_catalog", None)
        if adapter_catalog is not None and not isinstance(adapter_catalog, list):
            raise RuntimeConfigError("runtime_snapshot_invalid")
        capability_payload = snapshot_payload.get("capability_pack")
        legacy_minimal_capability = capability_payload == {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION
        }
        if run.runtime_config_version_id == LEGACY_RUNTIME_CONFIG_ID and legacy_minimal_capability:
            snapshot_payload["capability_pack"] = build_marketing_run_capability().model_dump(
                mode="json"
            )
        else:
            try:
                MarketingRunCapability.model_validate(capability_payload)
            except Exception as exc:
                raise RuntimeConfigError("runtime_snapshot_invalid") from exc
        try:
            snapshot = RuntimeConfigSnapshot.model_validate(snapshot_payload)
        except Exception as exc:
            raise RuntimeConfigError("runtime_snapshot_invalid") from exc
        if snapshot.config_version_id != run.runtime_config_version_id:
            raise RuntimeConfigError("runtime_snapshot_config_mismatch")
        config = await self.db.get(RuntimeConfigVersion, run.runtime_config_version_id)
        if config is None:
            if run.runtime_config_version_id == POC_RUNTIME_CONFIG_ID:
                poc_context = (run.prompt_snapshot_json or {}).get("pi_runtime_poc")
                if (
                    snapshot.runtime_backend != "pi"
                    or run.runtime_backend != "pi"
                    or snapshot.model.get("name") != "pi-poc"
                    or snapshot.datatap.get("service") != "poc"
                    or not isinstance(poc_context, dict)
                    or poc_context.get("runtime") != "pi"
                ):
                    raise RuntimeConfigError("runtime_snapshot_backend_mismatch")
                return snapshot
            raise RuntimeConfigError("runtime_config_not_found")
        if config.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
            raise RuntimeConfigError("runtime_contract_unsupported")
        if config.runtime_backend != snapshot.runtime_backend:
            raise RuntimeConfigError("runtime_snapshot_backend_mismatch")
        if run.runtime_backend != snapshot.runtime_backend:
            raise RuntimeConfigError("runtime_snapshot_backend_mismatch")
        if config.scope == "tenant" and config.tenant_id != run.tenant_id:
            raise RuntimeConfigError("runtime_snapshot_tenant_mismatch")
        if config.scope == "system" and config.tenant_id is not None:
            raise RuntimeConfigError("runtime_snapshot_tenant_mismatch")
        return snapshot

    async def resolve_secret_bundle(
        self, config_version_id: str, run_id: str
    ) -> RuntimeSecretBundle:
        run = await self.db.get(AgentRun, run_id)
        if run is None or run.runtime_config_version_id != config_version_id:
            raise RuntimeConfigError("runtime_secret_run_mismatch")
        config = await self.db.get(RuntimeConfigVersion, config_version_id)
        if config is None:
            raise RuntimeConfigError("runtime_secret_config_mismatch")
        if config.id == LEGACY_RUNTIME_CONFIG_ID:
            raise RuntimeConfigError("runtime_legacy_secret_unavailable")
        if (
            config.scope != "tenant"
            or config.tenant_id != run.tenant_id
            or config.runtime_backend != "pi"
            or run.runtime_backend != "pi"
        ):
            raise RuntimeConfigError("runtime_secret_config_mismatch")
        if not config.secret_refs_json:
            raise RuntimeConfigError("runtime_legacy_secret_unavailable")
        values: dict[str, SecretStr] = {}
        for ref in config.secret_refs_json:
            if not isinstance(ref, dict):
                raise RuntimeConfigError("runtime_secret_refs_invalid")
            secret_id = ref.get("secret_id")
            kind = ref.get("kind")
            key_version = ref.get("key_version")
            if not all(isinstance(item, str) and item for item in (secret_id, kind, key_version)):
                raise RuntimeConfigError("runtime_secret_refs_invalid")
            secret = await self.db.get(EncryptedRuntimeSecret, secret_id)
            if secret is None or secret.tenant_id != run.tenant_id or secret.status != "active":
                raise RuntimeConfigError("runtime_secret_missing")
            try:
                envelope = EncryptedSecretValue(
                    algorithm=secret.algorithm,
                    nonce=secret.nonce,
                    ciphertext=secret.ciphertext,
                    key_version=secret.key_version,
                    fingerprint=secret.fingerprint,
                    masked_value=secret.masked_value,
                )
                aad = _aad(run.tenant_id, secret_id, kind, key_version)
                value = self.cipher.decrypt(envelope, aad=aad)
            except Exception as exc:
                if isinstance(exc, RuntimeConfigError):
                    raise
                raise RuntimeConfigError("runtime_secret_decrypt_failed") from exc
            values[kind] = value
        try:
            urls = {
                kind.removeprefix("datatap_url:"): value
                for kind, value in values.items()
                if kind.startswith("datatap_url:")
            }
            return RuntimeSecretBundle(
                model_base_url=values["model_base_url"],
                model_api_key=values["model_api_key"],
                datatap_token=values["datatap_token"],
                datatap_urls=urls,
            )
        except KeyError as exc:
            raise RuntimeConfigError("runtime_secret_missing") from exc

    @staticmethod
    def poc_snapshot() -> RuntimeConfigSnapshot:
        capability = build_marketing_run_capability(model_version="pi-poc")
        return RuntimeConfigSnapshot(
            config_version_id=POC_RUNTIME_CONFIG_ID,
            runtime_contract_version=RUNTIME_CONTRACT_VERSION,
            runtime_backend="pi",
            model={"name": "pi-poc", "masked_origin": "poc"},
            datatap={"service": "poc", "schema_digest": "poc"},
            capability_pack=capability.model_dump(mode="json"),
            limits={"max_decisions": 50},
            billing={"mcp_call_points": 10},
        )

    async def _persist_secrets(
        self, tenant_id: str, config_id: str, bundle: RuntimeSecretBundle
    ) -> list[dict[str, str]]:
        values: list[tuple[str, SecretStr]] = [
            ("model_base_url", bundle.model_base_url),
            ("model_api_key", bundle.model_api_key),
            ("datatap_token", bundle.datatap_token),
            *[(f"datatap_url:{name}", value) for name, value in bundle.datatap_urls.items()],
        ]
        refs: list[dict[str, str]] = []
        for kind, plaintext in values:
            secret_id = f"secret-{uuid4().hex}"
            aad_prefix = f"{tenant_id}:{secret_id}:{kind}:"
            envelope = self.cipher.encrypt(plaintext, aad=f"{aad_prefix}{self.cipher.active_key_version}".encode())
            self.db.add(
                EncryptedRuntimeSecret(
                    id=secret_id,
                    tenant_id=tenant_id,
                    secret_kind=kind,
                    algorithm=envelope.algorithm,
                    nonce=envelope.nonce,
                    ciphertext=envelope.ciphertext,
                    key_version=envelope.key_version,
                    fingerprint=envelope.fingerprint,
                    masked_value=envelope.masked_value,
                    status="active",
                    created_at=self._now_fn(),
                )
            )
            refs.append(
                {
                    "secret_id": secret_id,
                    "kind": kind,
                    "key_version": envelope.key_version,
                }
            )
        await self.db.flush()
        return refs

    @staticmethod
    def _config_json(
        *,
        config_id: str,
        runtime_contract_version: str,
        runtime_backend: str,
        model: dict[str, Any],
        datatap: dict[str, Any],
        limits: dict[str, int | float],
        billing: dict[str, int | str],
    ) -> dict[str, Any]:
        if any(key not in {"name", "masked_origin", "provider"} for key in model):
            raise RuntimeConfigError("runtime_model_config_invalid")
        if set(datatap) - {"service", "schema_digest"}:
            raise RuntimeConfigError("runtime_datatap_config_invalid")
        capability = build_marketing_run_capability(model_version=str(model.get("name") or "runtime"))
        payload = {
            "config_version_id": config_id,
            "runtime_contract_version": runtime_contract_version,
            "runtime_backend": runtime_backend,
            "model": {"name": model.get("name"), "masked_origin": model.get("masked_origin"), "provider": model.get("provider")},
            "datatap": dict(datatap),
            "capability_pack": capability.model_dump(mode="json"),
            "limits": dict(limits),
            "billing": dict(billing),
        }
        if runtime_contract_version == RUNTIME_CONTRACT_VERSION:
            try:
                RuntimeConfigSnapshot.model_validate(payload)
            except Exception as exc:
                raise RuntimeConfigError("runtime_config_snapshot_invalid") from exc
        return payload

    @staticmethod
    def _snapshot_from_config(config: RuntimeConfigVersion) -> RuntimeConfigSnapshot:
        payload = copy.deepcopy(config.config_json or {})
        for key, expected in (
            ("config_version_id", config.id),
            ("runtime_contract_version", config.runtime_contract_version),
            ("runtime_backend", config.runtime_backend),
        ):
            if key in payload and payload[key] != expected:
                raise RuntimeConfigError("runtime_snapshot_invalid")
            payload[key] = expected
        capability_payload = payload.get("capability_pack")
        if config.id == LEGACY_RUNTIME_CONFIG_ID or not capability_payload:
            capability_payload = build_marketing_run_capability().model_dump(mode="json")
            payload["capability_pack"] = capability_payload
        else:
            try:
                MarketingRunCapability.model_validate(capability_payload)
            except Exception as exc:
                raise RuntimeConfigError("runtime_snapshot_invalid") from exc
        try:
            return RuntimeConfigSnapshot.model_validate(payload)
        except Exception as exc:
            raise RuntimeConfigError("runtime_snapshot_invalid") from exc


def _aad(tenant_id: str, secret_id: str, kind: str, key_version: str) -> bytes:
    return f"{tenant_id}:{secret_id}:{kind}:{key_version}".encode("utf-8")


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
