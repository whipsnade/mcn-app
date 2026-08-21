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


class SkillManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,95}$")
    revision: int = Field(gt=0)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(min_length=1, max_length=512)
    required_tools: tuple[str, ...] = ()
    artifact_contract: str | None = None
    content: str = Field(min_length=1, max_length=200_000)

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

    @model_validator(mode="after")
    def verify_manifest(self) -> SkillManifest:
        if len({entry.name for entry in self.entries}) != len(self.entries):
            raise ValueError("skill_snapshot_duplicate")
        if _manifest_digest(self.entries, self.source_scope) != self.manifest_digest:
            raise ValueError("skill_manifest_digest_mismatch")
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
            )
            for item in revisions
        )
        return cls(
            entries=entries,
            manifest_digest=_manifest_digest(entries, source_scope),
            source_scope=source_scope,
        )

    @classmethod
    def from_capability(
        cls,
        capability: MarketingRunCapability,
        *,
        source_scope: Literal["database_activation", "legacy_pack"],
    ) -> SkillManifest:
        if source_scope == "database_activation" and any(
            item.revision is None for item in capability.skills
        ):
            raise SkillSnapshotError("skill_snapshot_revision_missing")
        entries = tuple(
            SkillManifestEntry(
                name=item.name,
                revision=item.revision or 1,
                content_digest=item.digest,
                description=_description(item.content) or item.name,
                required_tools=item.required_tools,
                artifact_contract=item.artifact_contract,
                content=item.content,
            )
            for item in capability.skills
        )
        return cls(
            entries=entries,
            manifest_digest=_manifest_digest(entries, source_scope),
            source_scope=source_scope,
        )


class SkillSnapshotError(ValueError):
    """Fail-closed error raised before a Pi model or MCP dispatch."""


class SkillSnapshotService:
    """Resolve active DB pointers exactly once for a newly created Pi Run."""

    @staticmethod
    async def resolve_for_new_run(
        db: AsyncSession,
        *,
        tenant_id: str,
        base_capability: MarketingRunCapability,
        environment: str = "production",
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
            raise SkillSnapshotError("skill_snapshot_missing")
        resolved_by_name = {item.skill_name: item for item in resolved}
        skill_rows = tuple(
            MarketingSkillSnapshot(
                name=item.name,
                version=(
                    f"db-revision-{resolved_by_name[item.name].revision}"
                    if item.name in resolved_by_name
                    else item.version
                ),
                revision=(
                    resolved_by_name[item.name].revision
                    if item.name in resolved_by_name
                    else item.revision
                ),
                digest=(
                    resolved_by_name[item.name].content_digest
                    if item.name in resolved_by_name
                    else item.digest
                ),
                content=(
                    resolved_by_name[item.name].content
                    if item.name in resolved_by_name
                    else item.content
                ),
                required_tools=(
                    resolved_by_name[item.name].required_tools
                    if item.name in resolved_by_name
                    else item.required_tools
                ),
                artifact_contract=(
                    resolved_by_name[item.name].artifact_contract
                    if item.name in resolved_by_name
                    else item.artifact_contract
                ),
            )
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


def _manifest_digest(
    entries: tuple[SkillManifestEntry, ...], source_scope: str
) -> str:
    payload = {
        "source_scope": source_scope,
        "entries": [entry.model_dump(mode="json") for entry in entries],
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
    "SkillManifest",
    "SkillManifestEntry",
    "SkillSnapshotError",
    "SkillSnapshotService",
]
