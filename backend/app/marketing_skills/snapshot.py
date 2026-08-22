from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.marketing_capability_pack.runtime import MarketingRunCapability, MarketingSkillSnapshot
from app.marketing_skills.constants import MAX_SKILL_CONTENT_BYTES, MAX_SKILL_COUNT
from app.marketing_skills.models import SkillActivation
from app.marketing_skills.repository import (
    ResolvedSkillRevision,
    resolve_active_revisions,
)

if TYPE_CHECKING:
    from app.runtime_config.schemas import RuntimeConfigSnapshot

SKILL_MANIFEST_V1 = "skill_manifest_v1"
SKILL_MANIFEST_V2 = "skill_manifest_v2"
_DIRECT_MODEL_INPUT_V1 = "direct_model_input_v1"
_SOURCE_BOUND_INPUT_V2 = "source_bound_input_v2"
_ALLOWED_INPUT_CONTRACT_VERSIONS = frozenset({_DIRECT_MODEL_INPUT_V1, _SOURCE_BOUND_INPUT_V2})

# v1 digest 的七字段（历史 bytes 精确重现；新增字段绝不进入 v1 payload）。
_V1_ENTRY_FIELDS = (
    "name",
    "revision",
    "content_digest",
    "description",
    "required_tools",
    "artifact_contract",
    "content",
)


class SkillManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,95}$")
    revision: int = Field(gt=0)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(min_length=1, max_length=512)
    required_tools: tuple[str, ...] = ()
    artifact_contract: str | None = None
    content: str = Field(min_length=1, max_length=200_000)
    revision_id: str | None = Field(default=None, min_length=1, max_length=36)
    scope_key: str | None = Field(default=None, min_length=1, max_length=36)
    model_input_contract_version: str | None = None

    @model_validator(mode="after")
    def verify_content_digest(self) -> SkillManifestEntry:
        from app.marketing_skills.validation import canonical_skill_digest

        if len(self.content.encode("utf-8")) > MAX_SKILL_CONTENT_BYTES:
            raise ValueError("skill_snapshot_content_too_large")
        if canonical_skill_digest(self.content) != self.content_digest:
            raise ValueError("skill_snapshot_digest_mismatch")
        if len(set(self.required_tools)) != len(self.required_tools):
            raise ValueError("skill_snapshot_required_tools_duplicate")
        return self


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[SkillManifestEntry, ...] = ()
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope: Literal["database_activation", "legacy_pack"]
    schema_version: Literal["skill_manifest_v2"] | None = None

    @model_validator(mode="after")
    def verify_manifest(self) -> SkillManifest:
        if len({entry.name for entry in self.entries}) != len(self.entries):
            raise ValueError("skill_snapshot_duplicate")
        if self.schema_version == SKILL_MANIFEST_V2:
            for entry in self.entries:
                if (
                    entry.revision_id is None
                    or entry.scope_key is None
                    or entry.model_input_contract_version is None
                ):
                    raise ValueError("skill_manifest_v2_entry_field_missing")
                if entry.model_input_contract_version not in _ALLOWED_INPUT_CONTRACT_VERSIONS:
                    raise ValueError("skill_manifest_entry_contract_version_invalid")
        else:
            for entry in self.entries:
                if (
                    entry.revision_id is not None
                    or entry.scope_key is not None
                    or entry.model_input_contract_version is not None
                ):
                    raise ValueError("skill_manifest_v1_entry_field_present")
        if (
            _manifest_digest(self.entries, self.source_scope, self.schema_version)
            != self.manifest_digest
        ):
            raise ValueError("skill_manifest_digest_mismatch")
        if self.schema_version == SKILL_MANIFEST_V2:
            _assert_no_input_contract_conflict(self.entries)
        return self

    @classmethod
    def from_revisions(
        cls,
        revisions: tuple[ResolvedSkillRevision, ...],
        *,
        source_scope: Literal["database_activation", "legacy_pack"] = "database_activation",
    ) -> SkillManifest:
        entries = tuple(
            SkillManifestEntry(
                name=item.skill_name,
                revision=item.revision,
                content_digest=item.content_digest,
                description=item.description,
                required_tools=item.required_tools,
                artifact_contract=item.artifact_contract,
                content=item.content,
                revision_id=item.id,
                scope_key=_scope_key_of(item.tenant_id),
                model_input_contract_version=item.model_input_contract_version,
            )
            for item in revisions
        )
        return cls(
            entries=entries,
            manifest_digest=_manifest_digest(entries, source_scope, SKILL_MANIFEST_V2),
            source_scope=source_scope,
            schema_version=SKILL_MANIFEST_V2,
        )

    @classmethod
    def from_capability(
        cls,
        capability: MarketingRunCapability,
        *,
        source_scope: Literal["database_activation", "legacy_pack"],
    ) -> SkillManifest:
        """从 capability 重建 manifest：capability 携带 v2 身份字段时产 v2，否则产 v1。"""
        has_v2 = any(
            getattr(item, "revision_id", None) is not None for item in capability.skills
        )
        if has_v2 and any(
            getattr(item, "revision_id", None) is None for item in capability.skills
        ):
            raise SkillSnapshotError("skill_snapshot_revision_missing")
        if has_v2 and source_scope != "database_activation":
            raise SkillSnapshotError("skill_snapshot_invalid")
        entries = tuple(
            SkillManifestEntry(
                name=item.name,
                revision=item.revision or 1,
                content_digest=item.digest,
                description=_description(item.content) or item.name,
                required_tools=item.required_tools,
                artifact_contract=item.artifact_contract,
                content=item.content,
                revision_id=getattr(item, "revision_id", None),
                scope_key=getattr(item, "scope_key", None),
                model_input_contract_version=getattr(item, "model_input_contract_version", None),
            )
            for item in capability.skills
        )
        schema_version = SKILL_MANIFEST_V2 if has_v2 else None
        return cls(
            entries=entries,
            manifest_digest=_manifest_digest(entries, source_scope, schema_version),
            source_scope=source_scope,
            schema_version=schema_version,
        )


class SkillSnapshotError(ValueError):
    """Fail-closed error raised before a Pi model or MCP dispatch."""


def _scope_key_of(tenant_id: str | None) -> str:
    from app.marketing_skills.models import GLOBAL_SCOPE_KEY

    return tenant_id if tenant_id is not None else GLOBAL_SCOPE_KEY


def _assert_no_input_contract_conflict(entries: tuple[SkillManifestEntry, ...]) -> None:
    by_contract: dict[str, str] = {}
    for entry in entries:
        if entry.artifact_contract is None or entry.model_input_contract_version is None:
            continue
        existing = by_contract.setdefault(entry.artifact_contract, entry.model_input_contract_version)
        if existing != entry.model_input_contract_version:
            raise ValueError("skill_input_contract_conflict")


def manifest_artifact_input_contract_versions(manifest: SkillManifest) -> dict[str, str]:
    """从 v2 manifest 推导 artifact contract → 输入合同版本映射（含冲突复核）。"""
    if manifest.schema_version != SKILL_MANIFEST_V2:
        return {}
    _assert_no_input_contract_conflict(manifest.entries)
    return {
        entry.artifact_contract: entry.model_input_contract_version
        for entry in manifest.entries
        if entry.artifact_contract is not None and entry.model_input_contract_version is not None
    }


class SkillSnapshotService:
    """Resolve active DB pointers exactly once for a newly created Pi Run."""

    @staticmethod
    async def resolve_for_new_run(
        db: AsyncSession,
        *,
        tenant_id: str,
        base_capability: MarketingRunCapability,
        environment: str = "production",
        require_database_entries: bool = True,
    ) -> MarketingRunCapability:
        try:
            activation_names = tuple(
                (
                    await db.scalars(
                        select(SkillActivation.skill_name)
                        .where(
                            SkillActivation.environment == environment,
                            or_(
                                SkillActivation.tenant_id.is_(None),
                                SkillActivation.tenant_id == tenant_id,
                            ),
                        )
                        .distinct()
                        .order_by(SkillActivation.skill_name)
                    )
                ).all()
            )
            names = tuple(
                dict.fromkeys([item.name for item in base_capability.skills] + list(activation_names))
            )
            if len(names) > MAX_SKILL_COUNT:
                raise SkillSnapshotError("skill_snapshot_limit_exceeded")
            resolved = await resolve_active_revisions(
                db,
                tenant_id=tenant_id,
                skill_names=names,
                environment=environment,
            )
        except Exception as exc:
            # The broad wrapper keeps driver-specific missing-table/JSON errors
            # from becoming a silent static-pack fallback in a production Pi Run.
            if isinstance(exc, SkillSnapshotError):
                raise
            raise SkillSnapshotError("skill_snapshot_invalid") from exc
        if not resolved:
            if require_database_entries and base_capability.skills:
                # production fail-closed：capability 中任何 Skill 缺数据库
                # Activation/Revision 都不允许回退 package 正文。
                raise SkillSnapshotError("skill_activation_incomplete")
            raise SkillSnapshotError("skill_snapshot_missing")
        resolved_by_name = {item.skill_name: item for item in resolved}
        if require_database_entries:
            missing = [
                item.name
                for item in base_capability.skills
                if item.name not in resolved_by_name
            ]
            if missing:
                # production fail-closed：capability 中任何 Skill 缺数据库
                # Activation/Revision 都不允许回退 package 正文。
                raise SkillSnapshotError("skill_activation_incomplete")
        skill_rows = tuple(
            _capability_row(item, resolved_by_name.get(item.name))
            for item in base_capability.skills
        ) + tuple(
            MarketingSkillSnapshot(
                name=item.skill_name,
                version=f"db-revision-{item.revision}",
                revision=item.revision,
                digest=item.content_digest,
                content=item.content,
                required_tools=item.required_tools,
                artifact_contract=item.artifact_contract,
                revision_id=item.id,
                scope_key=_scope_key_of(item.tenant_id),
                model_input_contract_version=item.model_input_contract_version,
            )
            for item in resolved
            if item.skill_name not in {base.name for base in base_capability.skills}
        )
        payload = base_capability.model_dump(mode="json")
        payload["skills"] = [item.model_dump(mode="json") for item in skill_rows]
        return MarketingRunCapability.model_validate(payload)

    @staticmethod
    def manifest_from_capability(capability: MarketingRunCapability) -> SkillManifest:
        return SkillManifest.from_capability(capability, source_scope="database_activation")

    @staticmethod
    def validate_existing_run(snapshot: RuntimeConfigSnapshot) -> None:
        manifest = getattr(snapshot, "skill_manifest", None)
        if manifest is not None:
            SkillManifest.model_validate(manifest)


def _capability_row(
    item: MarketingSkillSnapshot, resolved: ResolvedSkillRevision | None
) -> MarketingSkillSnapshot:
    if resolved is None:
        return MarketingSkillSnapshot(
            name=item.name,
            version=item.version,
            revision=item.revision,
            digest=item.digest,
            content=item.content,
            required_tools=item.required_tools,
            artifact_contract=item.artifact_contract,
        )
    return MarketingSkillSnapshot(
        name=item.name,
        version=f"db-revision-{resolved.revision}",
        revision=resolved.revision,
        digest=resolved.content_digest,
        content=resolved.content,
        required_tools=resolved.required_tools,
        artifact_contract=resolved.artifact_contract,
        revision_id=resolved.id,
        scope_key=_scope_key_of(resolved.tenant_id),
        model_input_contract_version=resolved.model_input_contract_version,
    )


def _manifest_digest(
    entries: tuple[SkillManifestEntry, ...],
    source_scope: str,
    schema_version: str | None = None,
) -> str:
    if schema_version == SKILL_MANIFEST_V2:
        entry_payloads = [entry.model_dump(mode="json") for entry in entries]
        payload: dict[str, object] = {
            "schema_version": schema_version,
            "source_scope": source_scope,
            "entries": entry_payloads,
        }
    else:
        # v1：严格按历史七字段 bytes 计算（None 新字段绝不进入 payload）。
        entry_payloads = [
            {key: getattr(entry, key) for key in _V1_ENTRY_FIELDS} for entry in entries
        ]
        payload = {
            "source_scope": source_scope,
            "entries": entry_payloads,
        }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _description(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("description:"):
            return line.removeprefix("description:").strip()
    return ""


__all__ = [
    "SKILL_MANIFEST_V1",
    "SKILL_MANIFEST_V2",
    "SkillManifest",
    "SkillManifestEntry",
    "SkillSnapshotError",
    "SkillSnapshotService",
    "manifest_artifact_input_contract_versions",
]
