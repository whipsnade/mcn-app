from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.reporting.models import BiReport, Kol, KolSnapshot, TaskCandidate, UserKolFavorite
from app.reporting.schemas import (
    BiReportRead,
    BiReportSummary,
    CandidatePage,
    CandidateRead,
    FavoriteCreate,
    FavoriteRead,
)
from app.reporting.service import ReportingService


router = APIRouter()


def candidate_read(candidate: TaskCandidate, kol: Kol, snapshot: KolSnapshot) -> CandidateRead:
    dimensions = candidate.score_breakdown_json.get("dimensions", {})
    scores = {
        name: (value.get("raw_score") if isinstance(value, dict) else None)
        for name, value in dimensions.items()
    }
    normalized = snapshot.normalized_json
    return CandidateRead(
        id=candidate.id,
        kol_id=kol.id,
        platform=kol.platform,
        platform_account_id=kol.platform_account_id,
        nickname=normalized.get("nickname"),
        profile_url=kol.normalized_profile_url,
        rank=candidate.rank,
        total_score=float(candidate.total_score),
        scores=scores,
        matched_conditions=list(candidate.matched_conditions_json),
        risks=list(candidate.risk_flags_json),
        recommendation=candidate.recommendation_text,
        metrics={
            "followers": normalized.get("followers"),
            "quoted_price_cny": normalized.get("quoted_price_cny"),
            "collected_at": snapshot.collected_at.isoformat(),
            "data_completeness": candidate.score_breakdown_json.get("data_completeness"),
        },
    )


def bi_report_read(report: BiReport) -> BiReportRead:
    chart = report.chart_data_json
    return BiReportRead(
        id=report.id,
        task_id=report.task_id,
        report_version=report.report_version,
        candidate_version=report.candidate_version,
        overview=chart.get("overview", {}),
        score_composition=chart.get("score_composition", []),
        audience_content_fit=chart.get("audience_content_fit", {}),
        platform_distribution=chart.get("platform_distribution", []),
        budget_analysis=chart.get("budget_analysis", {}),
        comparison=chart.get("comparison", []),
        risks=chart.get("risks", []),
        conclusion=report.conclusion_text or "",
        sources=chart.get("sources", []),
        generated_at=report.completed_at or report.created_at,
    )


def bi_report_summary(report: BiReport) -> BiReportSummary:
    return BiReportSummary(
        id=report.id,
        task_id=report.task_id,
        report_version=report.report_version,
        candidate_version=report.candidate_version,
        status=report.status,
        generated_at=report.completed_at or report.created_at,
    )


def favorite_read(favorite: UserKolFavorite, kol: Kol) -> FavoriteRead:
    return FavoriteRead(
        kol_id=kol.id,
        platform=kol.platform,
        platform_account_id=kol.platform_account_id,
        profile_url=kol.normalized_profile_url,
        note=favorite.note,
        source_task_id=favorite.source_task_id,
        created_at=favorite.created_at,
    )


def not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


@router.get("/tasks/{task_id}/candidates", response_model=CandidatePage)
async def list_candidates(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    sort: Annotated[
        Literal["rank", "total", "audience", "content", "engagement", "budget", "growth", "brand_safety"],
        Query(),
    ] = "rank",
    direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
) -> CandidatePage:
    try:
        version, rows = await ReportingService(db).list_candidates(
            user.id, task_id, sort=sort, direction=direction
        )
    except LookupError as error:
        raise not_found("task_not_found") from error
    return CandidatePage(
        task_id=task_id,
        version=version,
        total=len(rows),
        items=[candidate_read(candidate, kol, snapshot) for candidate, kol, snapshot in rows],
    )


@router.get("/reports/{report_id}", response_model=BiReportRead)
async def get_report(
    report_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BiReportRead:
    try:
        report = await ReportingService(db).get_owned_report(user.id, report_id)
    except LookupError as error:
        raise not_found("report_not_found") from error
    return bi_report_read(report)


@router.get("/favorites", response_model=list[FavoriteRead])
async def list_favorites(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FavoriteRead]:
    rows = await ReportingService(db).list_favorites(user.id)
    return [favorite_read(favorite, kol) for favorite, kol in rows]


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
    return favorite_read(favorite, kol)


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
