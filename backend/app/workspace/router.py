from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.dependencies import CurrentUser
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
        metadata=message.metadata_json,
        created_at=message.created_at,
    )


async def session_read(
    service: WorkspaceService, workspace: WorkspaceSession, *, include_messages: bool
) -> SessionRead:
    messages = (
        await service.list_messages(workspace.user_id, workspace.id) if include_messages else []
    )
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
) -> SessionRead:
    service = WorkspaceService(db)
    workspace = await service.create_session(user.id, payload)
    return await session_read(service, workspace, include_messages=True)


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
    return await session_read(service, workspace, include_messages=True)


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
