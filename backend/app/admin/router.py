from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    AdminUserCreate,
    AdminUserItem,
    AdminUserListResponse,
    AdminUserUpdate,
    PointsAdjustRequest,
    PointsAdjustResponse,
    PointsHistoryResponse,
)
from app.admin.service import AdminService, PhoneConflictError
from app.billing.service import InsufficientPointsError
from app.core.errors import ErrorCode
from app.db.session import get_db
from app.identity.dependencies import AdminUser


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
) -> AdminUserItem:
    try:
        return await AdminService(db).create_user(admin, payload)
    except PhoneConflictError as error:
        raise phone_conflict(error) from error


@router.patch("/users/{user_id}", response_model=AdminUserItem)
async def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserItem:
    try:
        return await AdminService(db).update_user(admin, user_id, payload)
    except LookupError as error:
        raise not_found(error) from error
    except PhoneConflictError as error:
        raise phone_conflict(error) from error
    except ValueError as error:
        raise invalid(error) from error


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    try:
        await AdminService(db).disable_user(admin, user_id)
    except LookupError as error:
        raise not_found(error) from error
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
    return PointsAdjustResponse(
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
