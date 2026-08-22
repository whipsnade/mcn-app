from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.marketing_skills.models import SkillActivation, SkillRevision
from app.marketing_skills.validation import canonical_skill_digest


class SkillResolutionError(ValueError):
    """Raised when an active pointer cannot produce a trusted revision."""


@dataclass(frozen=True)
class ResolvedSkillRevision:
    id: str
    tenant_id: str | None
    skill_name: str
    revision: int
    content: str
    content_digest: str
    description: str
    required_tools: tuple[str, ...]
    artifact_contract: str | None
    model_input_contract_version: str = "direct_model_input_v1"


def stable_rollout_bucket(tenant_id: str, skill_name: str) -> int:
    value = f"{tenant_id}\0{skill_name}".encode("utf-8")
    return int(hashlib.sha256(value).hexdigest(), 16) % 100


def _is_selected(activation: SkillActivation, tenant_id: str, skill_name: str) -> bool:
    if activation.rollout_percent <= 0:
        return False
    if activation.rollout_percent >= 100:
        return True
    return stable_rollout_bucket(tenant_id, skill_name) < activation.rollout_percent


def _pick_activation(
    activations: Sequence[SkillActivation],
    *,
    tenant_id: str,
    skill_name: str,
) -> tuple[SkillActivation, str] | None:
    """Return the stable revision choice, including a rollout previous pointer.

    A tenant override owns its bucket.  When that bucket is outside the new
    rollout, the override's previous revision remains the effective revision;
    only an override without a previous revision falls through to the global
    activation.  This prevents a partial rollout from silently changing the
    whole skill pack for tenants outside the bucket.
    """

    ordered = (
        [item for item in activations if item.tenant_id == tenant_id]
        + [item for item in activations if item.tenant_id is None]
    )
    for item in ordered:
        if _is_selected(item, tenant_id, skill_name):
            return item, item.active_revision_id
        if item.previous_revision_id is not None:
            return item, item.previous_revision_id
    return None


async def resolve_active_revisions(
    db: AsyncSession,
    *,
    tenant_id: str,
    skill_names: Sequence[str],
    environment: str = "production",
) -> tuple[ResolvedSkillRevision, ...]:
    names = tuple(dict.fromkeys(skill_names))
    if not names:
        return ()
    activation_rows = list(
        (
            await db.scalars(
                select(SkillActivation)
                .where(
                    SkillActivation.environment == environment,
                    SkillActivation.skill_name.in_(names),
                    or_(SkillActivation.tenant_id.is_(None), SkillActivation.tenant_id == tenant_id),
                )
                .order_by(SkillActivation.updated_at.desc())
            )
        ).all()
    )
    by_name: dict[str, list[SkillActivation]] = {name: [] for name in names}
    for row in activation_rows:
        by_name.setdefault(row.skill_name, []).append(row)
    chosen = [
        picked
        for name in names
        if (picked := _pick_activation(by_name.get(name, ()), tenant_id=tenant_id, skill_name=name))
        is not None
    ]
    if not chosen:
        return ()

    revision_ids = {revision_id for _activation, revision_id in chosen}
    revisions = {
        row.id: row
        for row in (
            await db.scalars(select(SkillRevision).where(SkillRevision.id.in_(revision_ids)))
        ).all()
    }
    resolved: list[ResolvedSkillRevision] = []
    for activation, revision_id in chosen:
        revision = revisions.get(revision_id)
        if revision is None:
            raise SkillResolutionError("skill_revision_missing")
        if revision.skill_name != activation.skill_name:
            raise SkillResolutionError("skill_revision_name_mismatch")
        if revision.tenant_id not in (None, activation.tenant_id):
            raise SkillResolutionError("skill_revision_tenant_mismatch")
        if canonical_skill_digest(revision.content) != revision.content_digest:
            raise SkillResolutionError("skill_revision_digest_mismatch")
        required_tools = tuple(str(tool) for tool in (revision.required_tools or ()))
        resolved.append(
            ResolvedSkillRevision(
                id=revision.id,
                tenant_id=revision.tenant_id,
                skill_name=revision.skill_name,
                revision=revision.revision,
                content=revision.content,
                content_digest=revision.content_digest,
                description=revision.description,
                required_tools=required_tools,
                artifact_contract=revision.artifact_contract,
                model_input_contract_version=revision.model_input_contract_version,
            )
        )
    return tuple(resolved)


__all__ = [
    "ResolvedSkillRevision",
    "SkillResolutionError",
    "resolve_active_revisions",
    "stable_rollout_bucket",
]
