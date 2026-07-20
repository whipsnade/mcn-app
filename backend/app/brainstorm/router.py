from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.brainstorm.schemas import BrainstormRequest, BrainstormResponse
from app.brainstorm.service import BrainstormService
from app.core.errors import ErrorCode
from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.model.contracts import ModelAdapter, ModelAdapterError
from app.model.dependencies import get_model_adapter
from app.tasks.executor import TaskRunner
from app.tasks.router import get_task_runner
from app.tasks.service import TaskConflictError


router = APIRouter()


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
) -> BrainstormResponse:
    service = BrainstormService(db, model)
    try:
        outcome = await service.respond(user.id, session_id, payload)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found"
        ) from error
    except ModelAdapterError as error:
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
