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
from app.selection.models import SessionKolSelection
from app.selection.service import KolSelectionService


router = APIRouter()


def _selection_item(row: SessionKolSelection) -> dict[str, Any]:
    return {
        "platform": row.platform,
        "kol_uid": row.kol_uid,
        "nickname": row.nickname,
        "followers": row.followers,
        "city": row.city,
        "profile_url": row.profile_url,
        "fields": row.fields_json,
        "score": row.score_json,
    }


@router.get("/sessions/{session_id}/kol-selection")
async def list_kol_selection(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """圈选名单列表：按综合评分倒序，校验会话归属。"""
    try:
        total, rows = await KolSelectionService(db).list_selection(
            user_id=user.id, session_id=session_id, offset=offset, limit=limit
        )
    except LookupError as error:
        if str(error) == "session_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found"
            ) from error
        raise
    return {"total": total, "items": [_selection_item(row) for row in rows]}


@router.get("/sessions/{session_id}/kol-selection/export")
async def export_kol_selection(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """圈选名单 Excel 导出：模板渲染 4 sheet 工作簿。"""
    try:
        workbook = await export_session_selection(db, user.id, session_id)
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
