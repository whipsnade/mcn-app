import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.brainstorm.schemas import BrainstormRequest, BrainstormResponse
from app.brainstorm.service import BrainstormService
from app.core.errors import ErrorCode
from app.db.session import SessionFactory, get_db
from app.identity.dependencies import CurrentUser
from app.model.contracts import ModelAdapter, ModelAdapterError
from app.model.dependencies import get_model_adapter
from app.tasks.executor import TaskRunner
from app.tasks.router import get_task_runner
from app.tasks.service import TaskConflictError
from app.thinking.persistence import record_brainstorm_failure
from app.thinking.service import (
    SessionThinkingService,
    get_session_thinking_service,
)


router = APIRouter()
logger = logging.getLogger(__name__)


def brainstorm_model() -> ModelAdapter:
    """间接引用便于测试替换适配器；真实适配器沿用进程级缓存与超时配置。"""
    return get_model_adapter()


@router.post("/{session_id}/brainstorm", response_model=BrainstormResponse)
async def brainstorm(
    session_id: str,
    payload: BrainstormRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
    model: Annotated[ModelAdapter, Depends(brainstorm_model)],
    thinking_service: Annotated[
        SessionThinkingService, Depends(get_session_thinking_service)
    ],
) -> BrainstormResponse:
    user_id = user.id
    service = BrainstormService(db, model, thinking_service)
    try:
        outcome = await service.respond(user_id, session_id, payload)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found"
        ) from error
    except ModelAdapterError as error:
        try:
            blocks = await thinking_service.completed_blocks(
                turn_id=str(payload.turn_id),
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:
            logger.warning(
                "brainstorm_failure_thinking_read_failed session_id=%s",
                session_id,
                exc_info=True,
            )
            blocks = ()
        await db.rollback()
        await record_brainstorm_failure(
            SessionFactory,
            user_id=user_id,
            session_id=session_id,
            turn_id=str(payload.turn_id),
            user_content=payload.content,
            blocks=blocks,
            error_code=error.code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=ErrorCode.BRAINSTORM_MODEL_ERROR,
        ) from error
    except TaskConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="task_in_progress"
        ) from error
    await db.commit()
    if outcome.task_id is not None:
        task_runner.submit(outcome.task_id)
    return BrainstormResponse(
        ready=outcome.ready,
        task_id=outcome.task_id,
        message=outcome.message,
        profile=outcome.profile,
    )
