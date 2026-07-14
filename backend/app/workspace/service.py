from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workspace.models import Message, WorkspaceSession
from app.workspace.schemas import MessageCreate, SessionCreate, SessionUpdate


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class WorkspaceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_owned_session(
        self, user_id: str, session_id: str, *, for_update: bool = False
    ) -> WorkspaceSession:
        statement = select(WorkspaceSession).where(
            WorkspaceSession.id == session_id,
            WorkspaceSession.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        workspace = await self.db.scalar(statement)
        if workspace is None:
            raise LookupError("session_not_found")
        return workspace

    async def list_sessions(self, user_id: str) -> list[WorkspaceSession]:
        statement = (
            select(WorkspaceSession)
            .where(WorkspaceSession.user_id == user_id)
            .order_by(WorkspaceSession.last_accessed_at.desc())
        )
        return list((await self.db.scalars(statement)).all())

    async def list_messages(self, user_id: str, session_id: str) -> list[Message]:
        await self.get_owned_session(user_id, session_id)
        statement = (
            select(Message)
            .where(Message.session_id == session_id, Message.user_id == user_id)
            .order_by(Message.sequence.asc())
        )
        return list((await self.db.scalars(statement)).all())

    async def create_session(self, user_id: str, payload: SessionCreate) -> WorkspaceSession:
        now = utc_now()
        workspace = WorkspaceSession(
            id=str(uuid4()),
            user_id=user_id,
            title=f"{payload.brand}-{payload.campaign_name}",
            brand=payload.brand,
            campaign_name=payload.campaign_name,
            status="draft",
            platforms=list(payload.platforms),
            category=payload.category,
            target_audience=payload.target_audience,
            budget_min=payload.budget_min,
            budget_max=payload.budget_max,
            filters_snapshot=payload.filters,
            is_starred=False,
            last_accessed_at=now,
            created_at=now,
            updated_at=now,
        )
        self.db.add(workspace)
        await self.db.flush()
        self.db.add(
            Message(
                id=str(uuid4()),
                session_id=workspace.id,
                user_id=user_id,
                role="user",
                content=payload.initial_query,
                sequence=1,
                metadata_json={},
                created_at=now,
            )
        )
        await self.db.flush()
        return workspace

    async def append_message(
        self, user_id: str, session_id: str, payload: MessageCreate
    ) -> Message:
        workspace = await self.get_owned_session(user_id, session_id, for_update=True)
        max_sequence = await self.db.scalar(
            select(func.max(Message.sequence)).where(Message.session_id == session_id)
        )
        now = utc_now()
        message = Message(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            role="user",
            content=payload.content,
            sequence=(max_sequence or 0) + 1,
            metadata_json={},
            created_at=now,
        )
        workspace.last_accessed_at = now
        workspace.updated_at = now
        self.db.add(message)
        await self.db.flush()
        return message

    async def update_session(
        self, user_id: str, session_id: str, payload: SessionUpdate
    ) -> WorkspaceSession:
        workspace = await self.get_owned_session(user_id, session_id, for_update=True)
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            if value is not None:
                setattr(workspace, field, value)
        workspace.updated_at = utc_now()
        workspace.last_accessed_at = workspace.updated_at
        await self.db.flush()
        return workspace
