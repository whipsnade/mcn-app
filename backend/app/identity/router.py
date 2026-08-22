from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import REFRESH_COOKIE
from app.db.session import get_db
from app.identity.brand_profiles import BrandProfileService
from app.identity.dependencies import CurrentUser
from app.identity.models import UserChannelPermission
from app.identity.providers import MockSmsAuthProvider, MockWechatAuthProvider
from app.identity.schemas import (
    BrandProfileItem,
    BrandProfileList,
    BrandProfileDefaultSet,
    SmsCodeRequest,
    SmsCodeResponse,
    SmsLoginRequest,
    TokenResponse,
    UserRead,
    WechatLoginRequest,
)
from app.identity.service import IdentityService, LoginResult
from app.tenancy.models import Tenant
from app.tenancy.service import TenantService


auth_router = APIRouter()
users_router = APIRouter()


def set_refresh_cookie(response: Response, result: LoginResult) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        result.refresh_token,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        path="/api/v1/auth",
    )


def require_mock_mode() -> None:
    if get_settings().auth_mode != "mock":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


@auth_router.post("/mock/sms/code", response_model=SmsCodeResponse)
async def request_sms_code(payload: SmsCodeRequest) -> SmsCodeResponse:
    require_mock_mode()
    try:
        code = MockSmsAuthProvider().request_code(payload.phone)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return SmsCodeResponse(mock_code=code)


@auth_router.post("/mock/sms/login", response_model=TokenResponse)
async def login_with_sms(
    payload: SmsLoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    require_mock_mode()
    try:
        subject, nickname = MockSmsAuthProvider().verify(payload.phone, payload.code)
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    result = await login_with_retry(db, provider="sms", subject=subject, nickname=nickname)
    set_refresh_cookie(response, result)
    return TokenResponse(access_token=result.access_token)


@auth_router.post("/mock/wechat/login", response_model=TokenResponse)
async def login_with_wechat(
    payload: WechatLoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    require_mock_mode()
    try:
        subject, nickname = MockWechatAuthProvider().verify(payload.mock_ticket)
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    result = await login_with_retry(db, provider="wechat", subject=subject, nickname=nickname)
    set_refresh_cookie(response, result)
    return TokenResponse(access_token=result.access_token)


# 事务级瞬态 DB 错误：1213（死锁，MySQL 隐式回滚整个事务）与 1305（SAVEPOINT
# 不存在——锁竞争使事务在 SAVEPOINT 与后续语句之间被隐式回滚的表层形态）。
# 并发首登的多表建户链可偶发命中（本地 4 并发×60 轮复现 2/240），登录页不会
# 自动重试，用户侧表现为偶发一次 500。
_TRANSIENT_LOGIN_DB_ERROR_CODES = {1213, 1305}


def _is_transient_login_db_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "args", (None,))[0]
    return isinstance(code, int) and code in _TRANSIENT_LOGIN_DB_ERROR_CODES


async def login_with_retry(
    db: AsyncSession, *, provider: str, subject: str, nickname: str
) -> LoginResult:
    """执行登录；事务级瞬态 DB 错误回滚会话后单次幂等重试。

    幂等性由登录语义保证：重试时若用户已由并发赢家创建，走既有用户路径直接
    发新登录会话；否则正常完成建户。第二次仍失败则原样上抛，绝不第三次重试。
    """
    try:
        return await IdentityService(db).login(provider=provider, subject=subject, nickname=nickname)
    except OperationalError as exc:
        if not _is_transient_login_db_error(exc):
            raise
        await db.rollback()
        return await IdentityService(db).login(provider=provider, subject=subject, nickname=nickname)


@auth_router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenResponse:
    if refresh_token is None:
        raise HTTPException(status_code=401, detail="AUTH_EXPIRED")
    try:
        result = await IdentityService(db).refresh(refresh_token)
    except PermissionError as error:
        raise HTTPException(status_code=401, detail="AUTH_EXPIRED") from error
    set_refresh_cookie(response, result)
    return TokenResponse(access_token=result.access_token)


@auth_router.post("/logout", status_code=204)
async def logout(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    if refresh_token is not None:
        await IdentityService(db).revoke(refresh_token)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@users_router.get("/me", response_model=UserRead)
async def get_me(user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]) -> UserRead:
    channels = list(
        (
            await db.scalars(
                select(UserChannelPermission.channel).where(
                    UserChannelPermission.user_id == user.id,
                    UserChannelPermission.is_enabled.is_(True),
                )
            )
        ).all()
    )
    tenant_id: str | None = None
    tenant_name: str | None = None
    membership_role: str | None = None
    try:
        tenant_context = await TenantService(db).resolve_user(user.id)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="user_not_found") from error
    tenant_id = tenant_context.tenant_id
    membership_role = tenant_context.membership_role
    tenant_name = await db.scalar(
        select(Tenant.name).where(Tenant.id == tenant_context.tenant_id)
    )
    if tenant_name is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return UserRead(
        id=user.id,
        nickname=user.nickname,
        role=user.role,
        channels=channels,
        industries=[str(item) for item in (user.industries or ["美食"])],
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        membership_role=membership_role,
    )


def _brand_profile_list(rows) -> BrandProfileList:
    return BrandProfileList(
        items=[
            BrandProfileItem(brand_name=row.brand_name, is_default=row.is_default is True)
            for row in rows
        ]
    )


@users_router.get("/me/brand-profiles", response_model=BrandProfileList)
async def list_brand_profiles(
    user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> BrandProfileList:
    rows = await BrandProfileService(db).list_brand_profiles(user.id)
    return _brand_profile_list(rows)


@users_router.put("/me/brand-profiles", response_model=BrandProfileList)
async def set_default_brand_profile(
    payload: BrandProfileDefaultSet,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BrandProfileList:
    service = BrandProfileService(db)
    await service.set_default_brand(user.id, payload.brand_name)
    return _brand_profile_list(await service.list_brand_profiles(user.id))
