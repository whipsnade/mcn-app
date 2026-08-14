from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.admin.schemas import (
    AdminGatewayItem,
    AdminGatewayUpdate,
    AdminLicenseCreate,
    AdminLicenseItem,
    AdminLicenseStatusUpdate,
    AdminQuotaPolicyItem,
    AdminQuotaPolicyUpdate,
    AdminRunDiagnostics,
    AdminRuntimeConfigCreate,
    AdminRuntimeConfigItem,
    AdminTenantCreate,
    AdminTenantItem,
    AdminTenantUpdate,
    AdminTenantUserCreate,
    AdminTenantUserItem,
    AdminUsageResponse,
    AdminUserCreate,
    AdminUserItem,
    AdminUserListResponse,
    AdminUserUpdate,
    AdminWalletAdjustRequest,
    AdminWalletAdjustResponse,
    AdminWalletItem,
    AgentToolCallReconcileRequest,
    AgentToolCallReconcileResponse,
    PointsAdjustRequest,
    PointsAdjustResponse,
    PointsHistoryResponse,
)
from app.admin.service import AdminService, PhoneConflictError
from app.admin.gateway_service import GatewayAdminError, GatewayAdminService
from app.billing.service import InsufficientPointsError
from app.core.errors import ErrorCode
from app.db.session import get_db
from app.identity.dependencies import AdminUser
from app.tenancy.models import TenantMembership


router = APIRouter()


def not_found(error: LookupError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=ErrorCode.USER_NOT_FOUND
    )


def phone_conflict(error: PhoneConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail=ErrorCode.PHONE_CONFLICT
    )


def invalid(error: ValueError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=ErrorCode.VALIDATION_ERROR
    )


def gateway_error(error: GatewayAdminError) -> HTTPException:
    code = error.code
    if code.endswith("_not_found") or code in {"run_not_found"}:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=code)
    if code.endswith("_conflict") or code.endswith("_blocked") or code in {
        "tenant_disabled", "runtime_config_required", "runtime_secrets_required",
    }:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)
    if code.startswith("license_") or code == "feature_disabled":
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=code)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=code)


def _require_idempotency_key(value: str | None) -> str:
    """B6 管理写操作必须携带非空 Idempotency-Key（持久化唯一键回放/409）。"""
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="admin_idempotency_key_required"
        )
    key = value.strip()
    if len(key) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="admin_idempotency_key_invalid"
        )
    return key


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    keyword: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminUserListResponse:
    items, total = await AdminService(db).list_users(
        keyword=keyword, channel=channel, limit=limit, offset=offset
    )
    return AdminUserListResponse(items=items, total=total)


@router.post("/users", response_model=AdminUserItem, status_code=201)
async def create_user(
    payload: AdminUserCreate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminUserItem:
    try:
        result = await _gateway_service(db).create_legacy_user(
            admin, payload, idempotency_key=_require_idempotency_key(idempotency_key)
        )
        await db.commit()
        return result
    except PhoneConflictError as error:
        raise phone_conflict(error) from error
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc


@router.patch("/users/{user_id}", response_model=AdminUserItem)
async def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminUserItem:
    try:
        result = await _gateway_service(db).update_legacy_user(
            admin, user_id, payload, idempotency_key=_require_idempotency_key(idempotency_key)
        )
        await db.commit()
        return result
    except LookupError as error:
        raise not_found(error) from error
    except PhoneConflictError as error:
        raise phone_conflict(error) from error
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc
    except ValueError as error:
        raise invalid(error) from error


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    try:
        await _gateway_service(db).disable_legacy_user(
            admin, user_id, idempotency_key=_require_idempotency_key(idempotency_key)
        )
        await db.commit()
    except LookupError as error:
        raise not_found(error) from error
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc
    except ValueError as error:
        raise invalid(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/points", response_model=PointsAdjustResponse)
async def adjust_points(
    user_id: str,
    payload: PointsAdjustRequest,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> PointsAdjustResponse:
    try:
        wallet, transaction = await AdminService(db).adjust_points(
            admin,
            user_id,
            delta=payload.delta,
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    except LookupError as error:
        raise not_found(error) from error
    except InsufficientPointsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ErrorCode.INSUFFICIENT_POINTS
        ) from error
    except ValueError as error:
        raise invalid(error) from error
    tenant_id = await db.scalar(
        select(TenantMembership.tenant_id).where(
            TenantMembership.user_id == user_id,
            TenantMembership.status == "active",
        )
    )
    return PointsAdjustResponse(
        tenant_id=tenant_id,
        points=wallet.balance,
        reserved_points=wallet.reserved,
        transaction_id=transaction.id,
    )


@router.get("/users/{user_id}/points-history", response_model=PointsHistoryResponse)
async def points_history(
    user_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PointsHistoryResponse:
    try:
        items, total = await AdminService(db).points_history(
            user_id, limit=limit, offset=offset
        )
    except LookupError as error:
        raise not_found(error) from error
    return PointsHistoryResponse(items=items, total=total)


@router.post("/agent-tool-calls/{call_id}/reconcile", response_model=AgentToolCallReconcileResponse)
async def reconcile_agent_tool_call(
    call_id: str,
    payload: AgentToolCallReconcileRequest,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> AgentToolCallReconcileResponse:
    reconciler = getattr(request.app.state, "agent_tool_reconciler", None)
    try:
        outcome = await AdminService(
            db, tool_call_reconciler=reconciler
        ).reconcile_tool_call(
            admin,
            call_id,
            decision=payload.decision,
            note=payload.note,
            idempotency_key=idempotency_key,
        )
    except LookupError as error:
        raise not_found(error) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return AgentToolCallReconcileResponse(**outcome)


# ---------------------------------------------------------------------------
# Pi Agent Gateway administration (B6A)
# ---------------------------------------------------------------------------


def _gateway_service(db: AsyncSession) -> GatewayAdminService:
    return GatewayAdminService(db)


@router.get("/tenants", response_model=dict)
async def list_tenants(
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    del admin
    items, total = await _gateway_service(db).list_tenants(limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/tenants", response_model=AdminTenantItem, status_code=201)
async def create_tenant(
    payload: AdminTenantCreate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminTenantItem:
    try:
        result = await _gateway_service(db).create_tenant(admin, payload, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.patch("/tenants/{tenant_id}", response_model=AdminTenantItem)
async def update_tenant(
    tenant_id: str,
    payload: AdminTenantUpdate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminTenantItem:
    try:
        result = await _gateway_service(db).update_tenant(admin, tenant_id, payload, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.get("/tenants/{tenant_id}/users", response_model=dict)
async def list_tenant_users(
    tenant_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    del admin
    try:
        items, total = await _gateway_service(db).list_users(tenant_id, limit=limit, offset=offset)
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/tenants/{tenant_id}/users", response_model=AdminTenantUserItem, status_code=201)
async def create_tenant_user(
    tenant_id: str,
    payload: AdminTenantUserCreate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminTenantUserItem:
    try:
        result = await _gateway_service(db).create_user(admin, tenant_id, payload, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.get("/tenants/{tenant_id}/license", response_model=list[AdminLicenseItem])
async def list_tenant_licenses(
    tenant_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AdminLicenseItem]:
    del admin
    try:
        return await _gateway_service(db).list_licenses(tenant_id)
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc


@router.post("/tenants/{tenant_id}/license", response_model=AdminLicenseItem, status_code=201)
async def append_tenant_license(
    tenant_id: str,
    payload: AdminLicenseCreate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminLicenseItem:
    try:
        result = await _gateway_service(db).create_license(admin, tenant_id, payload, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.patch("/tenants/{tenant_id}/license/{license_id}", response_model=AdminLicenseItem)
async def update_tenant_license(
    tenant_id: str,
    license_id: str,
    payload: AdminLicenseStatusUpdate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminLicenseItem:
    try:
        result = await _gateway_service(db).update_license_status(admin, tenant_id, license_id, payload, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.get("/tenants/{tenant_id}/usage", response_model=AdminUsageResponse)
async def tenant_usage(
    tenant_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    group_by: Annotated[str, Query(pattern="^(tenant|user|run|day)$")] = "day",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminUsageResponse:
    del admin
    try:
        items = await _gateway_service(db).list_usage(tenant_id, group_by=group_by, limit=limit, offset=offset)
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc
    return AdminUsageResponse(items=items, limit=limit, offset=offset)


@router.get("/tenants/{tenant_id}/wallet", response_model=AdminWalletItem)
async def get_tenant_wallet(
    tenant_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminWalletItem:
    del admin
    try:
        return await _gateway_service(db).get_wallet(tenant_id)
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc


@router.post("/tenants/{tenant_id}/wallet/adjust", response_model=AdminWalletAdjustResponse)
async def adjust_tenant_wallet(
    tenant_id: str,
    payload: AdminWalletAdjustRequest,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminWalletAdjustResponse:
    try:
        result = await _gateway_service(db).adjust_wallet(
            admin, tenant_id, payload, idempotency_key=_require_idempotency_key(idempotency_key)
        )
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.get("/tenants/{tenant_id}/quota", response_model=dict)
async def list_tenant_quota(
    tenant_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    del admin
    try:
        items = await _gateway_service(db).list_quota(tenant_id)
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.put("/tenants/{tenant_id}/quota/{user_id}", response_model=AdminQuotaPolicyItem)
async def set_tenant_quota(
    tenant_id: str,
    user_id: str,
    payload: AdminQuotaPolicyUpdate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminQuotaPolicyItem:
    try:
        result = await _gateway_service(db).set_quota(
            admin, tenant_id, user_id, payload, idempotency_key=_require_idempotency_key(idempotency_key)
        )
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.get("/pi-runtime/gateways", response_model=dict)
async def list_pi_gateways(
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    del admin
    items, total = await _gateway_service(db).list_gateways(limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.patch("/pi-runtime/gateways/{gateway_id}", response_model=AdminGatewayItem)
async def update_pi_gateway(
    gateway_id: str,
    payload: AdminGatewayUpdate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminGatewayItem:
    try:
        result = await _gateway_service(db).update_gateway(admin, gateway_id, payload, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.get("/runtime-configs", response_model=dict)
async def list_runtime_configs(
    tenant_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    del admin
    try:
        items, total = await _gateway_service(db).list_runtime_configs(tenant_id, limit=limit, offset=offset)
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/runtime-configs", response_model=AdminRuntimeConfigItem, status_code=201)
async def create_runtime_config(
    payload: AdminRuntimeConfigCreate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminRuntimeConfigItem:
    try:
        result = await _gateway_service(db).create_runtime_config(admin, payload, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.post("/runtime-configs/{config_id}/activate", response_model=AdminRuntimeConfigItem)
async def activate_runtime_config(
    config_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminRuntimeConfigItem:
    try:
        result = await _gateway_service(db).activate_runtime_config(admin, config_id, idempotency_key=_require_idempotency_key(idempotency_key))
        await db.commit()
        return result
    except GatewayAdminError as exc:
        await db.rollback()
        raise gateway_error(exc) from exc


@router.get("/agent-runs/{run_id}/diagnostics", response_model=AdminRunDiagnostics)
async def run_diagnostics(
    run_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRunDiagnostics:
    try:
        return await _gateway_service(db).run_diagnostics(admin, run_id)
    except GatewayAdminError as exc:
        raise gateway_error(exc) from exc
