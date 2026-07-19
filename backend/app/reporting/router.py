from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.models import (
    AnalysisReport,
    Kol,
    KolSnapshot,
    UserKolFavorite,
)
from app.reporting.schemas import (
    AnalysisReportRead,
    AnalysisReportSummary,
    FavoriteCreate,
    FavoriteRead,
)
from app.reporting.service import ReportingService


router = APIRouter()


def analysis_report_read(report: AnalysisReport) -> AnalysisReportRead:
    return AnalysisReportRead(
        id=report.id,
        task_id=report.task_id,
        version=report.version,
        title=report.title,
        blocks=list(report.blocks_json),
        conclusion=report.conclusion_text,
        status=report.status,
        generated_at=report.created_at,
    )


def analysis_report_summary(report: AnalysisReport) -> AnalysisReportSummary:
    return AnalysisReportSummary(
        id=report.id,
        task_id=report.task_id,
        version=report.version,
        title=report.title,
        status=report.status,
        generated_at=report.created_at,
    )


def favorite_read(favorite: UserKolFavorite, kol: Kol, nickname: str | None = None) -> FavoriteRead:
    return FavoriteRead(
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


@router.get("/analysis-reports/{report_id}", response_model=AnalysisReportRead)
async def get_analysis_report(
    report_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnalysisReportRead:
    try:
        report = await AnalysisReportService(db).get_owned_report(user.id, report_id)
    except LookupError as error:
        raise not_found("report_not_found") from error
    return analysis_report_read(report)


@router.get("/favorites", response_model=list[FavoriteRead])
async def list_favorites(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FavoriteRead]:
    rows = await ReportingService(db).list_favorites(user.id)
    result: list[FavoriteRead] = []
    for favorite, kol in rows:
        snapshot = await db.scalar(
            select(KolSnapshot)
            .where(KolSnapshot.kol_id == kol.id)
            .order_by(desc(KolSnapshot.collected_at))
            .limit(1)
        )
        nickname = snapshot.normalized_json.get("nickname") if snapshot else None
        result.append(favorite_read(favorite, kol, nickname if isinstance(nickname, str) else None))
    return result


@router.post("/favorites", response_model=FavoriteRead, status_code=status.HTTP_201_CREATED)
async def create_favorite(
    payload: FavoriteCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> FavoriteRead:
    try:
        favorite, kol = await ReportingService(db).create_favorite(
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
    snapshot = await db.scalar(
        select(KolSnapshot)
        .where(KolSnapshot.kol_id == kol.id)
        .order_by(desc(KolSnapshot.collected_at))
        .limit(1)
    )
    nickname = snapshot.normalized_json.get("nickname") if snapshot else None
    return favorite_read(favorite, kol, nickname if isinstance(nickname, str) else None)


@router.delete("/favorites/{kol_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite(
    kol_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    try:
        await ReportingService(db).delete_favorite(user.id, kol_id)
    except LookupError as error:
        raise not_found("favorite_not_found") from error
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
