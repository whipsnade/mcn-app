from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.reporting.router import bi_report_summary
from app.reporting.schemas import CandidateVersionSummary, TaskAnalysisSummary
from app.reporting.service import ReportingService
from app.tasks.router import get_task_runner
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.tasks.executor import TaskRunner
from app.workspace.models import Message, WorkspaceSession
from app.workspace.schemas import MessageCreate, MessageRead, SessionCreate, SessionRead, SessionUpdate
from app.workspace.service import WorkspaceService


router = APIRouter()


def message_read(message: Message) -> MessageRead:
    return MessageRead(
        id=message.id,
        role=message.role,
        content=message.content,
        sequence=message.sequence,
        metadata=public_message_metadata(message.metadata_json),
        created_at=message.created_at,
    )


def public_message_metadata(metadata: dict) -> dict:
    """Expose only UI metadata; never return internal locks or raw provider data."""
    allowed = {
        "task_id",
        "status",
        "scoring_profile",
        "analysis_task_ids",
        "latest_analysis_task_id",
        "followup_suggestions_status",
        "followup_suggestions",
        "followup_suggestions_generated_at",
        "followup_suggestions_started_at",
        "followup_error",
    }
    return {key: value for key, value in metadata.items() if key in allowed}


async def session_read(
    service: WorkspaceService,
    workspace: WorkspaceSession,
    *,
    include_messages: bool,
    include_analysis: bool = False,
) -> SessionRead:
    messages = (
        await service.list_messages(workspace.user_id, workspace.id) if include_messages else []
    )
    latest_task = None
    latest_candidates = None
    latest_report = None
    if include_analysis:
        task, version, total, report = await ReportingService(service.db).latest_session_analysis(
            workspace.user_id, workspace.id
        )
        if task is not None:
            latest_task = TaskAnalysisSummary(
                id=task.id, status=task.status, completed_at=task.completed_at
            )
        if version is not None:
            latest_candidates = CandidateVersionSummary(
                task_id=task.id, version=version, total=total
            )
        if report is not None:
            latest_report = bi_report_summary(report)
    return SessionRead(
        id=workspace.id,
        title=workspace.title,
        brand=workspace.brand,
        campaign_name=workspace.campaign_name,
        status=workspace.status,
        platforms=workspace.platforms,
        category=workspace.category,
        target_audience=workspace.target_audience,
        budget_min=workspace.budget_min,
        budget_max=workspace.budget_max,
        filters=workspace.filters_snapshot,
        is_starred=workspace.is_starred,
        messages=[message_read(message) for message in messages],
        latest_task=latest_task,
        latest_candidates=latest_candidates,
        latest_report=latest_report,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def not_found(error: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found")


@router.post("", response_model=SessionRead, status_code=201)
async def create_session(
    payload: SessionCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
) -> SessionRead:
    service = WorkspaceService(db)
    workspace = await service.create_session(user.id, payload)
    initial_message = await db.scalar(
        select(Message).where(
            Message.session_id == workspace.id,
            Message.user_id == user.id,
            Message.sequence == 1,
        )
    )
    if initial_message is None:
        raise HTTPException(status_code=500, detail="initial_message_not_found")
    task = await TaskService(db).create(
        user.id,
        workspace.id,
        TaskCreate(content=payload.initial_query),
        trigger_message_id=initial_message.id,
    )
    await db.commit()
    task_runner.submit(task.id)
    return await session_read(service, workspace, include_messages=True, include_analysis=True)


@router.get("", response_model=list[SessionRead])
async def list_sessions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SessionRead]:
    service = WorkspaceService(db)
    workspaces = await service.list_sessions(user.id)
    return [await session_read(service, item, include_messages=False) for item in workspaces]


@router.get("/{session_id}", response_model=SessionRead)
async def get_session(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionRead:
    service = WorkspaceService(db)
    try:
        workspace = await service.get_owned_session(user.id, session_id)
    except LookupError as error:
        raise not_found(error) from error
    return await session_read(service, workspace, include_messages=True, include_analysis=True)


@router.patch("/{session_id}", response_model=SessionRead)
async def update_session(
    session_id: str,
    payload: SessionUpdate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionRead:
    service = WorkspaceService(db)
    try:
        workspace = await service.update_session(user.id, session_id, payload)
    except LookupError as error:
        raise not_found(error) from error
    return await session_read(service, workspace, include_messages=True)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    service = WorkspaceService(db)
    try:
        await service.delete_session(user.id, session_id)
    except LookupError as error:
        raise not_found(error) from error
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{session_id}/messages", response_model=MessageRead, status_code=201)
async def append_message(
    session_id: str,
    payload: MessageCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    service = WorkspaceService(db)
    try:
        message = await service.append_message(user.id, session_id, payload)
    except LookupError as error:
        raise not_found(error) from error
    return message_read(message)
