"""Tenant runtime configuration snapshots and per-Run secret resolution."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun
from app.agent_runtime.profiles import get_profile
from app.core.config import get_settings
from app.marketing_capability_pack.runtime import (
    MarketingRunCapability,
    build_marketing_run_capability,
)
from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
from app.marketing_skills.snapshot import (
    SkillManifest,
    SkillSnapshotError,
    SkillSnapshotService,
)
from app.pi_gateway.catalog import (
    PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES,
    PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES,
    canonical_adapter_catalog_bytes,
)
from app.tenancy.models import Tenant
from app.tenancy.service import effective_runtime_backend

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
        billing: dict[str, Any],
        secrets: RuntimeSecretBundle | None = None,
        runtime_contract_version: str = RUNTIME_CONTRACT_VERSION,
        environment: Literal["development", "staging", "production"] = "production",
        completion_mode: Literal["formal_analysis", "interaction"] = "formal_analysis",
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
                environment=environment,
                completion_mode=completion_mode,
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
        if config.runtime_backend == "pi" and config.runtime_contract_version == RUNTIME_CONTRACT_VERSION:
            self._validate_activation_capability_pack(config)
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

    @staticmethod
    def _validate_activation_capability_pack(config: RuntimeConfigVersion) -> None:
        """Validate the reviewed capability pack without a business target.

        Activation has no user Run profile.  It therefore validates only the
        immutable capability pack projection; artifact selection happens later
        inside the Pi decision boundary and is limited by the Run snapshot.
        """
        payload = copy.deepcopy(config.config_json or {})
        # Do not let the legacy admin mapping become a new Runtime policy.
        payload.pop("profile_artifact_contracts", None)
        capability_payload = payload.get("capability_pack")
        try:
            MarketingRunCapability.model_validate(capability_payload)
        except Exception as exc:
            raise RuntimeConfigError("runtime_capability_pack_invalid") from exc
        try:
            RuntimeConfigSnapshot.model_validate(payload)
        except Exception as exc:
            raise RuntimeConfigError("runtime_config_snapshot_invalid") from exc

    async def update_version(self, config_version_id: str, *, config_json: dict[str, Any]) -> None:
        del config_version_id, config_json
        raise RuntimeConfigError("runtime_config_immutable")

    async def snapshot_for_new_run(
        self, tenant_id: str, *, profile_name: str | None = None
    ) -> RuntimeConfigSnapshot:
        tenant = await self.db.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise RuntimeConfigError("runtime_tenant_not_found")
        effective_backend = effective_runtime_backend(
            tenant.runtime_backend,
            kill_switch=get_settings().pi_gateway_kill_switch,
        )
        config = None
        if not get_settings().pi_gateway_kill_switch and tenant.active_runtime_config_id is not None:
            config = await self.db.get(RuntimeConfigVersion, tenant.active_runtime_config_id)
            if config is None or config.scope != "tenant" or config.tenant_id != tenant_id:
                raise RuntimeConfigError("runtime_config_tenant_mismatch")
        if config is None:
            config = await self.db.scalar(
                select(RuntimeConfigVersion)
                .where(
                    RuntimeConfigVersion.scope == "system",
                    RuntimeConfigVersion.status == "active",
                    RuntimeConfigVersion.runtime_backend == effective_backend,
                )
                .order_by(RuntimeConfigVersion.version.desc())
                .limit(1)
            )
        if config is None or config.status != "active":
            raise RuntimeConfigError("runtime_config_required")
        if effective_backend == "pi" and (
            config.scope != "tenant" or config.runtime_backend != "pi"
        ):
            raise RuntimeConfigError("runtime_config_required")
        if config.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
            raise RuntimeConfigError("runtime_contract_unsupported")
        if config.runtime_backend == "pi" and not config.secret_refs_json:
            raise RuntimeConfigError("runtime_config_required")
        snapshot = self._snapshot_from_config(config, profile_name=profile_name)
        if snapshot.runtime_backend == "pi":
            try:
                base_capability = MarketingRunCapability.model_validate(snapshot.capability_pack)
                skill_capability = await SkillSnapshotService.resolve_for_new_run(
                    self.db,
                    tenant_id=tenant_id,
                    base_capability=base_capability,
                    environment=snapshot.environment,
                )
                skill_manifest = SkillSnapshotService.manifest_from_capability(skill_capability)
            except SkillSnapshotError as exc:
                raise RuntimeConfigError(str(exc)) from exc
            try:
                contract_versions = manifest_artifact_input_contract_versions(skill_manifest)
            except ValueError as exc:
                raise RuntimeConfigError(str(exc)) from exc
            snapshot = RuntimeConfigSnapshot.model_validate(
                {
                    **snapshot.model_dump(mode="json"),
                    "capability_pack": skill_capability.model_dump(mode="json"),
                    "skill_manifest": skill_manifest.model_dump(mode="json"),
                    **({"artifact_input_contract_versions": contract_versions} if contract_versions else {}),
                }
            )
            # Adapter bindings are part of the immutable Run snapshot as well:
            # claim may authenticate/lease the Run, but it cannot silently
            # append a live catalog after the Run has been created.
            adapter_catalog = await self._reviewed_adapter_catalog()
            snapshot = RuntimeConfigSnapshot.model_validate(
                {
                    **snapshot.model_dump(mode="json"),
                    "adapter_catalog": adapter_catalog,
                }
            )
        if snapshot.runtime_backend != effective_backend:
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
        adapter_catalog = snapshot_payload.get("adapter_catalog")
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
        # Legacy rows predate the explicit audit fields.  Enrich only the
        # in-memory pack aliases; the historical JSON is never rewritten.  Do
        # not infer an artifact target from the profile or old config mapping.
        capability_for_audit = snapshot_payload.get("capability_pack")
        if isinstance(capability_for_audit, Mapping):
            snapshot_payload.setdefault("profile_name", run.profile_name)
            snapshot_payload.setdefault(
                "capability_pack_version", capability_for_audit.get("pack_version")
            )
            snapshot_payload.setdefault(
                "capability_pack_manifest_digest", capability_for_audit.get("manifest_digest")
            )
        try:
            snapshot = RuntimeConfigSnapshot.model_validate(snapshot_payload)
        except Exception as exc:
            raise RuntimeConfigError("runtime_snapshot_invalid") from exc
        try:
            SkillSnapshotService.validate_existing_run(snapshot)
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

    async def snapshot_for_child_run(
        self, parent_run: AgentRun, *, profile_name: str
    ) -> RuntimeConfigSnapshot:
        """从父 Run 的同一不可变 config 版本选择子 Run 的 Profile 快照。

        internal Reviewer/utility 也是独立 Run，不能直接复用父 Run 的
        ``profile_name``/artifact contract；否则 terminal 会把子 Run 错当成父
        Run 的产物责任。这里读取的是父 Run 已固定的 config version，而不是
        当前 active config，并复用父快照中的 reviewed adapter catalog。
        """
        parent_snapshot = await self.snapshot_for_existing_run(parent_run)
        config = await self.db.get(RuntimeConfigVersion, parent_run.runtime_config_version_id)
        if config is None:
            raise RuntimeConfigError("runtime_child_snapshot_config_not_found")
        parent_capability = MarketingRunCapability.model_validate(parent_snapshot.capability_pack)
        child_snapshot = self._snapshot_from_config(
            config,
            profile_name=profile_name,
            capability_override=parent_capability,
            skill_manifest=parent_snapshot.skill_manifest,
        )
        if child_snapshot.runtime_backend != parent_snapshot.runtime_backend:
            raise RuntimeConfigError("runtime_child_snapshot_backend_mismatch")
        if (
            child_snapshot.capability_pack_version
            != parent_snapshot.capability_pack_version
            or child_snapshot.capability_pack_manifest_digest
            != parent_snapshot.capability_pack_manifest_digest
        ):
            raise RuntimeConfigError("runtime_child_snapshot_capability_mismatch")
        if child_snapshot.runtime_backend == "pi":
            child_snapshot = RuntimeConfigSnapshot.model_validate(
                {
                    **child_snapshot.model_dump(mode="json"),
                    "adapter_catalog": list(parent_snapshot.adapter_catalog),
                }
            )
        return child_snapshot

    async def _reviewed_adapter_catalog(self) -> list[dict[str, str]]:
        """Return the current reviewed adapter projection for a new Pi Run."""
        catalog_result = await self.db.execute(
            select(McpToolCatalog)
            .where(
                McpToolCatalog.review_status == "approved",
                McpToolCatalog.is_enabled.is_(True),
            )
            .order_by(McpToolCatalog.internal_tool_name)
        )
        catalogs = list(catalog_result.scalars().all())
        if len(catalogs) > PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES:
            # 这是 control-plane catalog 的防御性边界，不是模型可见工具数量
            # 或 Pi SDK 的业务限制；不要静默截断已审核能力。
            raise RuntimeConfigError("runtime_adapter_catalog_too_large")
        discovery_result = await self.db.execute(
            select(
                McpToolDiscovery.service_slug,
                McpToolDiscovery.discovery_digest,
                McpToolDiscovery.remote_name,
            ).where(McpToolDiscovery.review_status == "approved")
        )
        discovery_rows = list(discovery_result.all())
        remote_names: dict[tuple[str, str], set[str]] = {}
        for service_slug, discovery_digest, remote_name in discovery_rows:
            remote_names.setdefault((service_slug, discovery_digest), set()).add(remote_name)
        entries: list[dict[str, str]] = []
        for catalog in catalogs:
            digest = catalog.discovery_digest
            matches = remote_names.get((catalog.service_slug, digest), set())
            if len(matches) > 1:
                # discovery_digest 覆盖 remote name + input/output schema，正常两个
                # 工具不可能仅因 Schema 相同而共享 digest。该 fail-closed 检查防御的
                # 是异常数据库状态、重复审批绑定、数据损坏或极端 digest 冲突。
                raise RuntimeConfigError("runtime_adapter_catalog_ambiguous_remote")
            entries.append(
                {
                    "catalog_entry_id": catalog.id,
                    "adapter_visible_name": catalog.internal_tool_name,
                    "service": catalog.service_slug,
                    "remote_name": next(iter(matches), catalog.internal_tool_name),
                    "input_schema_digest": (
                        digest if digest.startswith("sha256:") else f"sha256:{digest}"
                    ),
                }
            )
        try:
            catalog_bytes = canonical_adapter_catalog_bytes(entries)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError("runtime_adapter_catalog_invalid") from exc
        if len(catalog_bytes) > PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES:
            raise RuntimeConfigError("runtime_adapter_catalog_too_large")
        return entries

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
            profile_name="session_analyst_v1",
            allowed_artifact_contracts=(
                "brand_report_v3",
                "campaign_report_v3",
                "insight_board_v1",
                "kol_selection_v3",
            ),
            capability_pack_version=capability.pack_version,
            capability_pack_manifest_digest=capability.manifest_digest,
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
        billing: dict[str, Any],
        environment: Literal["development", "staging", "production"] = "production",
        completion_mode: Literal["formal_analysis", "interaction"] = "formal_analysis",
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
            "environment": environment,
            "completion_mode": completion_mode,
            "model": {"name": model.get("name"), "masked_origin": model.get("masked_origin"), "provider": model.get("provider")},
            "datatap": dict(datatap),
            "capability_pack": capability.model_dump(mode="json"),
            "limits": dict(limits),
            "billing": dict(billing),
        }
        if runtime_contract_version == RUNTIME_CONTRACT_VERSION:
            try:
                snapshot_payload = dict(payload)
                RuntimeConfigSnapshot.model_validate(snapshot_payload)
            except Exception as exc:
                raise RuntimeConfigError("runtime_config_snapshot_invalid") from exc
        return payload

    @staticmethod
    def _snapshot_from_config(
        config: RuntimeConfigVersion,
        *,
        profile_name: str | None = None,
        capability_override: MarketingRunCapability | None = None,
        skill_manifest: SkillManifest | None = None,
    ) -> RuntimeConfigSnapshot:
        payload = copy.deepcopy(config.config_json or {})
        # Older config JSON may still contain this field.  It is deliberately
        # ignored for new Run snapshots and never copied into the snapshot.
        payload.pop("profile_artifact_contracts", None)
        # These fields are retained only so historical Run Snapshots can be
        # replayed. A new Run receives a candidate allowlist, never a fixed
        # required-artifact contract from an old config row.
        payload.pop("artifact_contract_mode", None)
        payload.pop("required_artifact_contract", None)
        for key, expected in (
            ("config_version_id", config.id),
            ("runtime_contract_version", config.runtime_contract_version),
            ("runtime_backend", config.runtime_backend),
        ):
            if key in payload and payload[key] != expected:
                raise RuntimeConfigError("runtime_snapshot_invalid")
            payload[key] = expected
        capability_payload = (
            capability_override.model_dump(mode="json")
            if capability_override is not None
            else payload.get("capability_pack")
        )
        if capability_override is not None:
            payload["capability_pack"] = capability_payload
        elif config.id == LEGACY_RUNTIME_CONFIG_ID or not capability_payload:
            capability_payload = build_marketing_run_capability().model_dump(mode="json")
            payload["capability_pack"] = capability_payload
        else:
            try:
                MarketingRunCapability.model_validate(capability_payload)
            except Exception as exc:
                raise RuntimeConfigError("runtime_snapshot_invalid") from exc
        capability = MarketingRunCapability.model_validate(capability_payload)
        payload["capability_pack_version"] = capability.pack_version
        payload["capability_pack_manifest_digest"] = capability.manifest_digest
        if profile_name is not None:
            try:
                get_profile(profile_name)
            except KeyError as exc:
                raise RuntimeConfigError("runtime_profile_invalid") from exc
            profile = get_profile(profile_name)
            payload["profile_name"] = profile_name
            payload["allowed_artifact_contracts"] = list(
                RuntimeConfigService._allowed_artifact_contracts(profile, capability)
            )
        if skill_manifest is not None:
            payload["skill_manifest"] = skill_manifest.model_dump(mode="json")
        try:
            return RuntimeConfigSnapshot.model_validate(payload)
        except Exception as exc:
            raise RuntimeConfigError("runtime_snapshot_invalid") from exc

    @staticmethod
    def _allowed_artifact_contracts(profile, capability: MarketingRunCapability) -> tuple[str, ...]:
        """Return the immutable candidate contract set for one Run profile."""
        available = {
            str(item.get("artifact_type"))
            for item in capability.artifact_contracts
            if isinstance(item, Mapping)
        }
        skill_contracts = {skill.artifact_contract for skill in capability.skills}
        approved = available | skill_contracts
        return tuple(sorted(set(profile.allowed_artifact_contracts) & approved))


def _aad(tenant_id: str, secret_id: str, kind: str, key_version: str) -> bytes:
    return f"{tenant_id}:{secret_id}:{kind}:{key_version}".encode("utf-8")


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
