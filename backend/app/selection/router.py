from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode
from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.model.contracts import ModelAdapter, ModelAdapterError
from app.model.dependencies import get_model_adapter
from app.reporting.router import analysis_report_read
from app.reporting.schemas import AnalysisReportRead
from app.selection.analysis import run_kol_analysis
from app.selection.exporter import export_session_selection
from app.selection.service import KolSelectionService, serialize_selection_item


router = APIRouter()


@router.get("/sessions/{session_id}/kol-selection")
async def list_kol_selection(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    set_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """圈选名单列表：缺省读最新 selection set；set_id 切换历史名单。"""
    service = KolSelectionService(db)
    try:
        selection_set = await service.resolve_selection_set(
            user_id=user.id, session_id=session_id, selection_set_id=set_id
        )
        if selection_set is None:
            total, rows = 0, []
        else:
            total, rows = await service.list_selection_items(
                user_id=user.id,
                selection_set_id=selection_set.id,
                offset=offset,
                limit=limit,
            )
    except LookupError as error:
        detail = str(error)
        if detail in {"session_not_found", "selection_set_not_found"}:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=detail
            ) from error
        raise
    return {"total": total, "items": [serialize_selection_item(row) for row in rows]}


@router.get("/sessions/{session_id}/selection-sets")
async def list_selection_sets(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    """名单版本列表（version desc）。"""
    try:
        sets = await KolSelectionService(db).list_selection_sets(
            user_id=user.id, session_id=session_id
        )
    except LookupError as error:
        if str(error) == "session_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found"
            ) from error
        raise
    return [
        {
            "set_id": selection_set.id,
            "title": selection_set.title,
            "version": selection_set.version,
            "status": selection_set.status,
            "item_count": item_count,
            "created_at": selection_set.created_at.isoformat(),
        }
        for selection_set, item_count in sets
    ]


@router.get("/sessions/{session_id}/kol-top10-trend")
async def list_kol_top10_trend(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    set_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """KOL 分析页趋势图：指定名单版本的 rank 1–10，缺省读最新版本。"""
    try:
        resolved_set_id, items = await KolSelectionService(db).list_top10_trend(
            user_id=user.id,
            session_id=session_id,
            selection_set_id=set_id,
        )
    except LookupError as error:
        if str(error) in {"session_not_found", "selection_set_not_found"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        raise
    return {"set_id": resolved_set_id, "items": items}


@router.get("/sessions/{session_id}/kol-selection/export")
async def export_kol_selection(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    set_id: Annotated[str | None, Query()] = None,
) -> Response:
    """圈选名单 Excel 导出：缺省导最新 set；set_id 切换历史名单。"""
    try:
        workbook = await export_session_selection(db, user.id, session_id, set_id=set_id)
    except LookupError as error:
        code = str(error)
        if code in {"session_not_found", "selection_set_not_found"}:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=code
            ) from error
        if code == "no_kol_selection":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="NO_KOL_SELECTION"
            ) from error
        raise
    return Response(
        content=workbook.content,
        media_type=workbook.content_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(workbook.filename)}"
            ),
        },
    )


def kol_analysis_model() -> ModelAdapter:
    """间接引用便于测试替换模型适配器。"""
    return get_model_adapter()


@router.post("/sessions/{session_id}/kol-analysis", response_model=AnalysisReportRead)
async def create_kol_analysis(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[ModelAdapter, Depends(kol_analysis_model)],
) -> AnalysisReportRead:
    """手动触发 KOL 圈选分析：代码聚合 + 模型撰写会话级报告，零 MCP 零积分。"""
    try:
        report = await run_kol_analysis(
            db, model, user_id=user.id, session_id=session_id
        )
    except LookupError as error:
        code = str(error)
        if code == "session_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=code
            ) from error
        if code == "no_kol_selection":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="NO_KOL_SELECTION"
            ) from error
        if code == "report_version_conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="REPORT_VERSION_CONFLICT"
            ) from error
        raise
    except ModelAdapterError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=ErrorCode.KOL_ANALYSIS_MODEL_ERROR,
        ) from error
    await db.commit()
    return analysis_report_read(report)
