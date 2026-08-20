from __future__ import annotations

import difflib
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.gateway_service import GatewayAdminError, GatewayAdminService
from app.identity.models import User
from app.marketing_skills.models import SkillActivation, SkillRevision
from app.marketing_skills.schemas import (
    SkillActivationRead,
    SkillActivationRequest,
    SkillDetailRead,
    SkillDiffRead,
    SkillListItem,
    SkillListRead,
    SkillRevisionCreate,
    SkillRevisionRead,
    SkillRollbackRequest,
    SkillValidationErrorRead,
    SkillValidationRead,
    SkillValidationRequest,
)
from app.marketing_skills.validation import canonical_skill_digest, validate_skill_content
from app.mcp_gateway.models import McpToolCatalog
from app.tenancy.models import Tenant


SkillAdminError = GatewayAdminError


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SkillAdminService:
    def __init__(self, db: AsyncSession, *, approved_tools: Iterable[str] | None = None) -> None:
        self.db = db
        self._admin_service = GatewayAdminService(db)
        self._approved_tools_override = (
            frozenset(str(name) for name in approved_tools)
            if approved_tools is not None
            else None
        )

    async def _approved_tools(self) -> frozenset[str]:
        if self._approved_tools_override is not None:
            return self._approved_tools_override
        rows = await self.db.scalars(
            select(McpToolCatalog.internal_tool_name).where(
                McpToolCatalog.review_status == "approved",
                McpToolCatalog.is_enabled.is_(True),
            )
        )
        return frozenset(rows.all())

    async def validate(self, payload: SkillValidationRequest) -> SkillValidationRead:
        result = validate_skill_content(
            payload.content,
            expected_name=payload.expected_name,
            approved_tools=await self._approved_tools(),
        )
        return SkillValidationRead(
            valid=result.valid,
            name=result.name,
            description=result.description,
            required_tools=list(result.required_tools),
            artifact_contract=result.artifact_contract,
            content_digest=result.content_digest,
            errors=[
                SkillValidationErrorRead(code=item.code, message=item.message, line=item.line)
                for item in result.errors
            ],
        )

    async def _ensure_tenant(self, tenant_id: str | None) -> None:
        if tenant_id is not None and await self.db.get(Tenant, tenant_id) is None:
            raise SkillAdminError("tenant_not_found")

    async def create_revision(
        self,
        admin: User,
        skill_name: str,
        payload: SkillRevisionCreate,
        *,
        idempotency_key: str,
    ) -> SkillRevisionRead:
        async def produce() -> tuple[SkillRevisionRead, str]:
            await self._ensure_tenant(payload.tenant_id)
            result = validate_skill_content(
                payload.content,
                expected_name=skill_name,
                approved_tools=await self._approved_tools(),
            )
            if not result.valid:
                raise SkillAdminError("skill_validation_failed")
            latest = await self.db.scalar(
                select(SkillRevision)
                .where(
                    SkillRevision.skill_name == skill_name,
                    SkillRevision.tenant_id == payload.tenant_id,
                )
                .order_by(SkillRevision.revision.desc())
                .limit(1)
                .with_for_update()
            )
            revision_number = (latest.revision if latest is not None else 0) + 1
            row = SkillRevision(
                id=str(uuid4()),
                tenant_id=payload.tenant_id,
                skill_name=skill_name,
                revision=revision_number,
                content=result.normalized_content,
                content_digest=result.content_digest,
                description=result.description or "",
                required_tools=list(result.required_tools),
                artifact_contract=result.artifact_contract,
                created_by=admin.id,
                created_at=_now(),
                change_note=payload.change_note,
            )
            self.db.add(row)
            await self.db.flush()
            self._admin_service._audit(
                admin.id,
                action="skill.revision_create",
                target_type="skill_revision",
                target_id=row.id,
                detail={
                    "skill_name": row.skill_name,
                    "tenant_id": row.tenant_id,
                    "revision": row.revision,
                    "content_digest": row.content_digest,
                    "change_note": row.change_note,
                },
                idempotency_key=idempotency_key,
            )
            return self._revision_read(row), row.id

        return await self._admin_service._idempotent(
            admin,
            action="skill.revision_create",
            idempotency_key=idempotency_key,
            fingerprint={
                "skill_name": skill_name,
                "tenant_id": payload.tenant_id,
                "content_digest": canonical_skill_digest(payload.content),
                "change_note": payload.change_note,
            },
            target_type="skill_revision",
            response_model=SkillRevisionRead,
            produce=produce,
        )

    async def list_skills(self) -> SkillListRead:
        revisions = list(
            (
                await self.db.scalars(
                    select(SkillRevision).order_by(
                        SkillRevision.skill_name, SkillRevision.revision.desc()
                    )
                )
            ).all()
        )
        activations = list(
            (
                await self.db.scalars(
                    select(SkillActivation).order_by(
                        SkillActivation.skill_name, SkillActivation.updated_at.desc()
                    )
                )
            ).all()
        )
        by_name: dict[str, list[SkillRevision]] = {}
        for row in revisions:
            by_name.setdefault(row.skill_name, []).append(row)
        active_by_name: dict[str, list[SkillActivationRead]] = {}
        for row in activations:
            active_by_name.setdefault(row.skill_name, []).append(
                await self._activation_read(row)
            )
        items = [
            SkillListItem(
                skill_name=name,
                latest_revision=rows[0].revision,
                revision_count=len(rows),
                active=active_by_name.get(name, []),
            )
            for name, rows in by_name.items()
        ]
        return SkillListRead(items=items, total=len(items))

    async def detail(self, skill_name: str) -> SkillDetailRead:
        revisions = list(
            (
                await self.db.scalars(
                    select(SkillRevision)
                    .where(SkillRevision.skill_name == skill_name)
                    .order_by(SkillRevision.revision.desc())
                )
            ).all()
        )
        if not revisions:
            raise SkillAdminError("skill_not_found")
        activations = list(
            (
                await self.db.scalars(
                    select(SkillActivation)
                    .where(SkillActivation.skill_name == skill_name)
                    .order_by(SkillActivation.updated_at.desc())
                )
            ).all()
        )
        return SkillDetailRead(
            skill_name=skill_name,
            revisions=[self._revision_read(row) for row in revisions],
            activations=[await self._activation_read(row) for row in activations],
        )

    async def _revision_for_number(
        self, skill_name: str, revision: int, *, tenant_id: str | None = None
    ) -> SkillRevision:
        rows = list(
            (
                await self.db.scalars(
                    select(SkillRevision).where(
                        SkillRevision.skill_name == skill_name,
                        SkillRevision.revision == revision,
                        or_(SkillRevision.tenant_id.is_(None), SkillRevision.tenant_id == tenant_id),
                    )
                )
            ).all()
        )
        if not rows:
            raise SkillAdminError("skill_revision_not_found")
        return next((row for row in rows if row.tenant_id == tenant_id), rows[0])

    async def diff(self, skill_name: str, *, from_revision: int, to_revision: int) -> SkillDiffRead:
        from_row = await self._revision_for_number(skill_name, from_revision)
        to_row = await self._revision_for_number(skill_name, to_revision)
        diff = "".join(
            difflib.unified_diff(
                from_row.content.splitlines(keepends=True),
                to_row.content.splitlines(keepends=True),
                fromfile=f"{skill_name}@{from_revision}",
                tofile=f"{skill_name}@{to_revision}",
            )
        )
        return SkillDiffRead(
            skill_name=skill_name,
            from_revision=from_revision,
            to_revision=to_revision,
            diff=diff,
        )

    async def activate(
        self,
        admin: User,
        skill_name: str,
        payload: SkillActivationRequest,
        *,
        idempotency_key: str,
    ) -> SkillActivationRead:
        async def produce() -> tuple[SkillActivationRead, str]:
            await self._ensure_tenant(payload.tenant_id)
            revision = await self._revision_for_number(
                skill_name, payload.revision, tenant_id=payload.tenant_id
            )
            if revision.tenant_id not in (None, payload.tenant_id):
                raise SkillAdminError("skill_revision_not_found")
            condition = [
                SkillActivation.environment == payload.environment,
                SkillActivation.skill_name == skill_name,
            ]
            condition.append(
                SkillActivation.tenant_id.is_(None)
                if payload.tenant_id is None
                else SkillActivation.tenant_id == payload.tenant_id
            )
            activation = await self.db.scalar(
                select(SkillActivation).where(*condition).with_for_update()
            )
            if activation is None:
                activation = SkillActivation(
                    id=str(uuid4()),
                    environment=payload.environment,
                    tenant_id=payload.tenant_id,
                    skill_name=skill_name,
                    active_revision_id=revision.id,
                    previous_revision_id=None,
                    rollout_percent=payload.rollout_percent,
                    updated_by=admin.id,
                    updated_at=_now(),
                )
                self.db.add(activation)
            else:
                if activation.active_revision_id != revision.id:
                    activation.previous_revision_id = activation.active_revision_id
                activation.active_revision_id = revision.id
                activation.rollout_percent = payload.rollout_percent
                activation.updated_by = admin.id
                activation.updated_at = _now()
            await self.db.flush()
            self._admin_service._audit(
                admin.id,
                action="skill.activate",
                target_type="skill_activation",
                target_id=activation.id,
                detail={
                    "skill_name": skill_name,
                    "environment": payload.environment,
                    "tenant_id": payload.tenant_id,
                    "active_revision": revision.revision,
                    "rollout_percent": payload.rollout_percent,
                },
                idempotency_key=idempotency_key,
            )
            return await self._activation_read(activation), activation.id

        return await self._admin_service._idempotent(
            admin,
            action="skill.activate",
            idempotency_key=idempotency_key,
            fingerprint={"skill_name": skill_name, **payload.model_dump(mode="json")},
            target_type="skill_activation",
            response_model=SkillActivationRead,
            produce=produce,
        )

    async def rollback(
        self,
        admin: User,
        skill_name: str,
        payload: SkillRollbackRequest,
        *,
        idempotency_key: str,
    ) -> SkillActivationRead:
        async def produce() -> tuple[SkillActivationRead, str]:
            await self._ensure_tenant(payload.tenant_id)
            condition = [
                SkillActivation.environment == payload.environment,
                SkillActivation.skill_name == skill_name,
            ]
            condition.append(
                SkillActivation.tenant_id.is_(None)
                if payload.tenant_id is None
                else SkillActivation.tenant_id == payload.tenant_id
            )
            activation = await self.db.scalar(
                select(SkillActivation).where(*condition).with_for_update()
            )
            if activation is None or activation.previous_revision_id is None:
                raise SkillAdminError("skill_rollback_unavailable")
            current_revision_id = activation.active_revision_id
            activation.active_revision_id = activation.previous_revision_id
            activation.previous_revision_id = current_revision_id
            activation.updated_by = admin.id
            activation.updated_at = _now()
            await self.db.flush()
            self._admin_service._audit(
                admin.id,
                action="skill.rollback",
                target_type="skill_activation",
                target_id=activation.id,
                detail={
                    "skill_name": skill_name,
                    "environment": payload.environment,
                    "tenant_id": payload.tenant_id,
                    "active_revision_id": activation.active_revision_id,
                    "previous_revision_id": activation.previous_revision_id,
                },
                idempotency_key=idempotency_key,
            )
            return await self._activation_read(activation), activation.id

        return await self._admin_service._idempotent(
            admin,
            action="skill.rollback",
            idempotency_key=idempotency_key,
            fingerprint={"skill_name": skill_name, **payload.model_dump(mode="json")},
            target_type="skill_activation",
            response_model=SkillActivationRead,
            produce=produce,
        )

    async def _activation_read(self, row: SkillActivation) -> SkillActivationRead:
        active = await self.db.get(SkillRevision, row.active_revision_id)
        previous = (
            await self.db.get(SkillRevision, row.previous_revision_id)
            if row.previous_revision_id
            else None
        )
        if active is None:
            raise SkillAdminError("skill_revision_missing")
        return SkillActivationRead(
            id=row.id,
            environment=row.environment,
            tenant_id=row.tenant_id,
            skill_name=row.skill_name,
            active_revision=active.revision,
            active_revision_id=active.id,
            previous_revision=previous.revision if previous else None,
            previous_revision_id=previous.id if previous else None,
            rollout_percent=row.rollout_percent,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _revision_read(row: SkillRevision) -> SkillRevisionRead:
        return SkillRevisionRead(
            id=row.id,
            tenant_id=row.tenant_id,
            skill_name=row.skill_name,
            revision=row.revision,
            content=row.content,
            content_digest=row.content_digest,
            description=row.description,
            required_tools=[str(item) for item in (row.required_tools or [])],
            artifact_contract=row.artifact_contract,
            created_by=row.created_by,
            created_at=row.created_at,
            change_note=row.change_note,
        )


__all__ = ["SkillAdminError", "SkillAdminService"]
