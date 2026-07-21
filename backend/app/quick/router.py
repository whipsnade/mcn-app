from typing import Annotated

from functools import lru_cache

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import InsufficientPointsError
from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.identity.models import User, UserChannelPermission
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.transport import McpTransport
from app.model.contracts import ModelAdapter, ModelAdapterError
from app.model.dependencies import get_model_adapter
from app.quick.schemas import (
    EvaluateResponse,
    KolDetailResponse,
    KolRecommendationsResponse,
    TopPostsResponse,
)
from app.quick.service import (
    DATASOURCE_BY_PLATFORM,
    KOL_SEARCH_TOOLS,
    MAX_UPLOAD_BYTES,
    QuickCallFailedError,
    QuickService,
)


router = APIRouter()


@lru_cache
def _quick_mcp_transport() -> McpTransport:
    """快捷功能专用传输层：30s 读取超时（快速失败），独立的熔断器与并发
    状态，与任务运行时的 60s 传输层互不污染。"""
    settings = get_settings()
    return DataTapTransport(
        token=settings.datatap_mcp_token,
        read_timeout_seconds=30.0,
    )


def quick_transport() -> McpTransport:
    """间接引用便于测试替换传输层；真实传输为快捷专用实例。"""
    return _quick_mcp_transport()


def quick_model() -> ModelAdapter:
    """间接引用便于测试替换模型适配器。"""
    return get_model_adapter()


def insufficient(error: InsufficientPointsError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail=ErrorCode.INSUFFICIENT_POINTS
    )


def call_failed(error: QuickCallFailedError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail=ErrorCode.QUICK_CALL_FAILED
    )


def invalid(error: ValueError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=ErrorCode.VALIDATION_ERROR
    )


async def _default_platforms(db: AsyncSession, user: User) -> list[str]:
    """默认平台 = 用户启用渠道 ∩ 五平台 KOL 搜索，缺省小红书+抖音。"""
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
    selected = [channel for channel in channels if channel in KOL_SEARCH_TOOLS]
    return selected or ["xiaohongshu", "douyin"]


def _parse_platforms(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    if not selected or any(item not in KOL_SEARCH_TOOLS for item in selected):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ErrorCode.VALIDATION_ERROR,
        )
    return list(dict.fromkeys(selected))


@router.get("/kol-recommendations", response_model=KolRecommendationsResponse)
async def kol_recommendations(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    transport: Annotated[McpTransport, Depends(quick_transport)],
    model: Annotated[ModelAdapter, Depends(quick_model)],
    budget: Annotated[int, Query(ge=10000, le=500000)],
    platforms: Annotated[str | None, Query()] = None,
) -> KolRecommendationsResponse:
    selected = _parse_platforms(platforms) or await _default_platforms(db, user)
    service = QuickService(db, transport=transport, model=model)
    try:
        items, points = await service.kol_recommendations(
            user, budget=budget, platforms=selected
        )
    except InsufficientPointsError as error:
        raise insufficient(error) from error
    except QuickCallFailedError as error:
        raise call_failed(error) from error
    return KolRecommendationsResponse(items=items, points_cost=points)


@router.get("/kol-detail", response_model=KolDetailResponse)
async def kol_detail(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    transport: Annotated[McpTransport, Depends(quick_transport)],
    model: Annotated[ModelAdapter, Depends(quick_model)],
    platform: Annotated[str, Query()],
    kw_uid: Annotated[str, Query(min_length=1, max_length=128)],
    nickname: Annotated[str, Query(min_length=1, max_length=200)],
) -> KolDetailResponse:
    if platform not in DATASOURCE_BY_PLATFORM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ErrorCode.VALIDATION_ERROR,
        )
    service = QuickService(db, transport=transport, model=model)
    try:
        detail, posts, posts_degraded, points = await service.kol_detail(
            user, platform=platform, kw_uid=kw_uid, nickname=nickname
        )
    except InsufficientPointsError as error:
        raise insufficient(error) from error
    except QuickCallFailedError as error:
        raise call_failed(error) from error
    return KolDetailResponse(
        detail=detail, posts=posts, points_cost=points, posts_degraded=posts_degraded
    )


@router.get("/top-posts", response_model=TopPostsResponse)
async def top_posts(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    transport: Annotated[McpTransport, Depends(quick_transport)],
    model: Annotated[ModelAdapter, Depends(quick_model)],
    platform: Annotated[str, Query()],
) -> TopPostsResponse:
    if platform not in ("xiaohongshu", "douyin"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ErrorCode.VALIDATION_ERROR,
        )
    service = QuickService(db, transport=transport, model=model)
    try:
        items, points = await service.top_posts(user, platform=platform)
    except InsufficientPointsError as error:
        raise insufficient(error) from error
    except QuickCallFailedError as error:
        raise call_failed(error) from error
    # degraded/fallback_kols 字段保留契约：模型小循环下不再做跨网关降级，恒为空。
    return TopPostsResponse(items=items, points_cost=points, degraded=False, fallback_kols=[])


@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[ModelAdapter, Depends(quick_model)],
    file: Annotated[UploadFile, File()],
) -> EvaluateResponse:
    content = await file.read()
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ErrorCode.VALIDATION_ERROR,
        )
    service = QuickService(db, model=model)
    try:
        document = await service.evaluate(
            user, filename=file.filename or "", content=content
        )
    except ValueError as error:
        raise invalid(error) from error
    except ModelAdapterError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=ErrorCode.QUICK_CALL_FAILED
        ) from error
    return EvaluateResponse(
        title=document.title, analysis_markdown=document.analysis_markdown
    )
