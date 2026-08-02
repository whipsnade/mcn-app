"""收藏 API 路由：/favorites 契约不变（list/create/delete + platform+kol_uid 幂等）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.favorites.schemas import FavoriteCreate, FavoriteRead
from app.favorites.service import FavoritesService, serialize_selection_item
from app.identity.dependencies import CurrentUser
from app.reporting.models import Kol, KolSnapshot, UserKolFavorite


router = APIRouter()


def favorite_read(
    favorite: UserKolFavorite, kol: Kol | None, nickname: str | None = None
) -> FavoriteRead:
    if kol is None:
        # 新路径行：nickname/snapshot 直接读列，不再查 KolSnapshot。
        return FavoriteRead(
            id=favorite.id,
            kol_id=None,
            nickname=favorite.nickname or None,
            platform=favorite.platform or "",
            kol_uid=favorite.kol_uid,
            snapshot=favorite.snapshot_json,
            note=favorite.note,
            source_task_id=favorite.source_task_id,
            created_at=favorite.created_at,
        )
    return FavoriteRead(
        id=favorite.id,
        kol_id=kol.id,
        nickname=nickname,
        platform=kol.platform,
        platform_account_id=kol.platform_account_id,
        profile_url=kol.normalized_profile_url,
        note=favorite.note,
        source_task_id=favorite.source_task_id,
        created_at=favorite.created_at,
    )


def not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def _latest_kol_nickname(db: AsyncSession, kol_id: str) -> str | None:
    snapshot = await db.scalar(
        select(KolSnapshot)
        .where(KolSnapshot.kol_id == kol_id)
        .order_by(desc(KolSnapshot.collected_at))
        .limit(1)
    )
    if snapshot is None:
        return None
    nickname = snapshot.normalized_json.get("nickname")
    return nickname if isinstance(nickname, str) else None


@router.get("/favorites", response_model=list[FavoriteRead])
async def list_favorites(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FavoriteRead]:
    rows = await FavoritesService(db).list_favorites(user.id)
    result: list[FavoriteRead] = []
    for favorite, kol in rows:
        if kol is None:
            result.append(favorite_read(favorite, None))
            continue
        nickname = await _latest_kol_nickname(db, kol.id)
        result.append(favorite_read(favorite, kol, nickname))
    return result


@router.get("/favorites/kol-selection-ref")
async def get_favorite_kol_selection_ref(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    platform: Annotated[str, Query(min_length=1, max_length=32)],
    kol_uid: Annotated[str, Query(min_length=1, max_length=128)],
) -> dict:
    """把收藏达人解析到其最新圈选名单条目，供前端复用圈选详情弹窗与版本缓存。

    收藏本身不绑定名单；按 user_id+platform+kol_uid 取最新 selection set 中的条目。
    该达人不在任何圈选名单（如快捷推荐里收藏的）时返回 404，前端回退快捷详情。
    """
    try:
        selection_set, item = await FavoritesService(db).resolve_selection_ref(
            user.id, platform=platform, kol_uid=kol_uid
        )
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    return {
        "session_id": selection_set.session_id,
        "set_id": selection_set.id,
        "item": serialize_selection_item(item),
    }


@router.post("/favorites", response_model=FavoriteRead, status_code=status.HTTP_201_CREATED)
async def create_favorite(
    payload: FavoriteCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> FavoriteRead:
    service = FavoritesService(db)
    if payload.kol_id is None:
        # platform+kol_uid 新路径：幂等 upsert，不校验 TaskCandidate。
        favorite = await service.create_favorite_by_key(
            user.id,
            platform=payload.platform or "",
            kol_uid=payload.kol_uid or "",
            nickname=payload.nickname,
            snapshot=payload.snapshot,
        )
        await db.commit()
        # 统一返回 200，使幂等的重复收藏和首次创建具有一致的客户端处理方式。
        response.status_code = status.HTTP_200_OK
        return favorite_read(favorite, None)
    try:
        favorite, kol = await service.create_favorite(
            user.id,
            kol_id=payload.kol_id,
            note=payload.note,
            source_task_id=payload.source_task_id,
        )
    except LookupError as error:
        raise not_found(str(error)) from error
    await db.commit()
    # 统一返回 200，使幂等的重复收藏和首次创建具有一致的客户端处理方式。
    response.status_code = status.HTTP_200_OK
    nickname = await _latest_kol_nickname(db, kol.id)
    return favorite_read(favorite, kol, nickname)


@router.delete("/favorites", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite_by_key(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    platform: Annotated[str, Query(min_length=1, max_length=32)],
    kol_uid: Annotated[str, Query(min_length=1, max_length=128)],
) -> Response:
    try:
        await FavoritesService(db).delete_favorite_by_key(user.id, platform, kol_uid)
    except LookupError as error:
        raise not_found("favorite_not_found") from error
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/favorites/{kol_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite(
    kol_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    try:
        await FavoritesService(db).delete_favorite(user.id, kol_id)
    except LookupError as error:
        raise not_found("favorite_not_found") from error
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
