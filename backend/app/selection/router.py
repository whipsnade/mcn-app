from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode
from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.model.contracts import ModelAdapter, ModelAdapterError
from app.model.dependencies import get_model_adapter
from app.reporting.router import analysis_report_read
from app.reporting.schemas import AnalysisReportRead
from app.selection.analysis import run_kol_analysis


router = APIRouter()


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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="REPORT_VERSION_CONFLICT"
        ) from error
    except ModelAdapterError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=ErrorCode.KOL_ANALYSIS_MODEL_ERROR,
        ) from error
    await db.commit()
    return analysis_report_read(report)
