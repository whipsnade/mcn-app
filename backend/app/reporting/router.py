from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.service import ArtifactService
from app.core.errors import ErrorCode
from app.db.session import get_db
from app.goals.models import TaskGoal
from app.identity.dependencies import CurrentUser
from app.model.contracts import ModelAdapter, ModelAdapterError
from app.model.dependencies import get_model_adapter
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.builders import (
    collect_goal_evidence,
    run_brand_analysis,
    run_campaign_analysis,
)
from app.reporting.models import (
    AnalysisReport,
    Kol,
    KolSnapshot,
    UserKolFavorite,
)
from app.reporting.schemas import (
    AnalysisReportRead,
    AnalysisReportSummary,
    AnalysisRetryRequest,
    FavoriteCreate,
    FavoriteRead,
    SessionReportItem,
    SessionReportType,
)
from app.reporting.service import ReportingService
from app.selection.models import KolSelectionItem, KolSelectionSet
from app.selection.service import serialize_selection_item
from app.tasks.models import AnalysisTask
from app.workspace.models import WorkspaceSession


router = APIRouter()
logger = logging.getLogger(__name__)

# report_type → (artifact_type, 构建器)
_ANALYSIS_RETRY_BUILDERS = {
    "brand_analysis": ("brand_report", run_brand_analysis),
    "campaign_analysis": ("campaign_report", run_campaign_analysis),
}


def analysis_model() -> ModelAdapter:
    """间接引用便于测试替换模型适配器。"""
    return get_model_adapter()


def analysis_report_read(report: AnalysisReport) -> AnalysisReportRead:
    return AnalysisReportRead(
        id=report.id,
        task_id=report.task_id,
        report_type=report.report_type,
        scope=report.scope_json,
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


@router.get("/sessions/{session_id}/reports", response_model=list[SessionReportItem])
async def list_session_reports(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    report_type: Annotated[SessionReportType, Query()],
) -> list[SessionReportItem]:
    """按类型列出会话报告版本（version desc）。"""
    session = await db.get(WorkspaceSession, session_id)
    if session is None or session.user_id != user.id or session.deleted_at is not None:
        raise not_found("session_not_found")
    reports = list(
        (
            await db.scalars(
                select(AnalysisReport)
                .where(
                    AnalysisReport.session_id == session_id,
                    AnalysisReport.report_type == report_type,
                )
                .order_by(AnalysisReport.version.desc())
            )
        ).all()
    )
    return [
        SessionReportItem(
            report_id=report.id,
            title=report.title,
            version=report.version,
            scope=report.scope_json,
            status=report.status,
            created_at=report.created_at,
        )
        for report in reports
    ]


@router.post("/sessions/{session_id}/analysis-retry", response_model=AnalysisReportRead)
async def retry_analysis(
    session_id: str,
    payload: AnalysisRetryRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[ModelAdapter, Depends(analysis_model)],
) -> AnalysisReportRead:
    """品牌/活动报告手动重试：重跑构建器（不调 MCP、零积分），同步返回不发 SSE。

    找会话最近一个该 goal_type 且轨迹证据非空的 task_goal；无则 409 NO_EVIDENCE。
    """
    session = await db.get(WorkspaceSession, session_id)
    if session is None or session.user_id != user.id or session.deleted_at is not None:
        raise not_found("session_not_found")
    goals = list(
        (
            await db.scalars(
                select(TaskGoal)
                .join(AnalysisTask, AnalysisTask.id == TaskGoal.task_id)
                .where(
                    AnalysisTask.session_id == session_id,
                    TaskGoal.goal_type == payload.report_type,
                )
                .order_by(TaskGoal.created_at.desc())
            )
        ).all()
    )
    target: tuple[AnalysisTask, TaskGoal] | None = None
    for goal in goals:
        task = await db.get(AnalysisTask, goal.task_id)
        if task is not None and collect_goal_evidence(task.plan_json):
            target = (task, goal)
            break
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="NO_EVIDENCE"
        )
    task, goal = target
    artifact_type, builder = _ANALYSIS_RETRY_BUILDERS[payload.report_type]
    try:
        report = await builder(
            db,
            model,
            user_id=user.id,
            session_id=session_id,
            task=task,
            goal=goal,
        )
    except ModelAdapterError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=ErrorCode.ANALYSIS_MODEL_ERROR,
        ) from error
    try:
        # 手动重试产物登记 manual Artifact（artifact_key 含 report_id，天然幂等）。
        await ArtifactService(db).register_artifact(
            user_id=user.id,
            session_id=session_id,
            artifact_key=f"manual:{report.id}:{artifact_type}",
            artifact_type=artifact_type,
            title=report.title,
            version=report.version,
            status=report.status,
            task_id=None,
            report_id=report.id,
            scope=report.scope_json,
        )
    except Exception:
        logger.warning(
            "analysis_retry_artifact_register_failed report_id=%s", report.id, exc_info=True
        )
    await db.commit()
    return analysis_report_read(report)


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
        if kol is None:
            result.append(favorite_read(favorite, None))
            continue
        snapshot = await db.scalar(
            select(KolSnapshot)
            .where(KolSnapshot.kol_id == kol.id)
            .order_by(desc(KolSnapshot.collected_at))
            .limit(1)
        )
        nickname = snapshot.normalized_json.get("nickname") if snapshot else None
        result.append(favorite_read(favorite, kol, nickname if isinstance(nickname, str) else None))
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
    row = (
        await db.execute(
            select(KolSelectionItem, KolSelectionSet)
            .join(KolSelectionSet, KolSelectionItem.selection_set_id == KolSelectionSet.id)
            .where(
                KolSelectionItem.user_id == user.id,
                KolSelectionItem.platform == platform,
                KolSelectionItem.kol_uid == kol_uid,
            )
            .order_by(desc(KolSelectionSet.created_at))
            .limit(1)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="selection_ref_not_found")
    item, selection_set = row
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
    service = ReportingService(db)
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
    snapshot = await db.scalar(
        select(KolSnapshot)
        .where(KolSnapshot.kol_id == kol.id)
        .order_by(desc(KolSnapshot.collected_at))
        .limit(1)
    )
    nickname = snapshot.normalized_json.get("nickname") if snapshot else None
    return favorite_read(favorite, kol, nickname if isinstance(nickname, str) else None)


@router.delete("/favorites", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite_by_key(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    platform: Annotated[str, Query(min_length=1, max_length=32)],
    kol_uid: Annotated[str, Query(min_length=1, max_length=128)],
) -> Response:
    try:
        await ReportingService(db).delete_favorite_by_key(user.id, platform, kol_uid)
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
        await ReportingService(db).delete_favorite(user.id, kol_id)
    except LookupError as error:
        raise not_found("favorite_not_found") from error
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
