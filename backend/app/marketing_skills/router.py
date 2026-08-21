from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.dependencies import AdminUser
from app.marketing_skills.schemas import (
    SkillActivationRead,
    SkillActivationRequest,
    SkillDetailRead,
    SkillDiffRead,
    SkillListRead,
    SkillRevisionCreate,
    SkillRevisionRead,
    SkillRollbackRequest,
    SkillValidationRead,
    SkillValidationRequest,
)
from app.marketing_skills.service import SkillAdminError, SkillAdminService


router = APIRouter()


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise HTTPException(status_code=400, detail="admin_idempotency_key_required")
    value = value.strip()
    if len(value) > 128:
        raise HTTPException(status_code=400, detail="admin_idempotency_key_invalid")
    return value


def _skill_error(error: SkillAdminError) -> HTTPException:
    if error.code.endswith("_not_found"):
        return HTTPException(status_code=404, detail=error.code)
    if error.code.endswith("_conflict") or error.code.endswith("_unavailable"):
        return HTTPException(status_code=409, detail=error.code)
    if error.code == "skill_validation_failed":
        return HTTPException(status_code=422, detail=error.code)
    return HTTPException(status_code=400, detail=error.code)


@router.get("", response_model=SkillListRead)
async def list_skills(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillListRead:
    return await SkillAdminService(db).list_skills()


@router.post("/validate", response_model=SkillValidationRead)
async def validate_skill(
    payload: SkillValidationRequest,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillValidationRead:
    return await SkillAdminService(db).validate(payload)


@router.get("/{skill_name}/diff", response_model=SkillDiffRead)
async def skill_diff(
    skill_name: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    from_revision: Annotated[int, Query(gt=0)],
    to_revision: Annotated[int, Query(gt=0)],
    from_revision_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    to_revision_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    tenant_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
) -> SkillDiffRead:
    try:
        return await SkillAdminService(db).diff(
            skill_name,
            from_revision=from_revision,
            to_revision=to_revision,
            tenant_id=tenant_id,
            from_revision_id=from_revision_id,
            to_revision_id=to_revision_id,
        )
    except SkillAdminError as error:
        raise _skill_error(error) from error


@router.get("/{skill_name}/revisions/{revision}", response_model=SkillRevisionRead)
async def get_skill_revision(
    skill_name: str,
    revision: int,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillRevisionRead:
    try:
        row = await SkillAdminService(db)._revision_for_number(skill_name, revision)
        return SkillAdminService._revision_read(row)
    except SkillAdminError as error:
        raise _skill_error(error) from error


@router.post("/{skill_name}/revisions", response_model=SkillRevisionRead, status_code=201)
async def create_skill_revision(
    skill_name: str,
    payload: SkillRevisionCreate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SkillRevisionRead:
    try:
        result = await SkillAdminService(db).create_revision(
            admin,
            skill_name,
            payload,
            idempotency_key=_idempotency_key(idempotency_key),
        )
        await db.commit()
        return result
    except SkillAdminError as error:
        await db.rollback()
        raise _skill_error(error) from error


@router.post("/{skill_name}/activate", response_model=SkillActivationRead)
async def activate_skill(
    skill_name: str,
    payload: SkillActivationRequest,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SkillActivationRead:
    try:
        result = await SkillAdminService(db).activate(
            admin,
            skill_name,
            payload,
            idempotency_key=_idempotency_key(idempotency_key),
        )
        await db.commit()
        return result
    except SkillAdminError as error:
        await db.rollback()
        raise _skill_error(error) from error


@router.post("/{skill_name}/rollback", response_model=SkillActivationRead)
async def rollback_skill(
    skill_name: str,
    payload: SkillRollbackRequest,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SkillActivationRead:
    try:
        result = await SkillAdminService(db).rollback(
            admin,
            skill_name,
            payload,
            idempotency_key=_idempotency_key(idempotency_key),
        )
        await db.commit()
        return result
    except SkillAdminError as error:
        await db.rollback()
        raise _skill_error(error) from error


@router.get("/{skill_name}", response_model=SkillDetailRead)
async def get_skill(
    skill_name: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillDetailRead:
    try:
        return await SkillAdminService(db).detail(skill_name)
    except SkillAdminError as error:
        raise _skill_error(error) from error


__all__ = ["router"]
