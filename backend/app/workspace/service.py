from datetime import UTC, datetime
import re
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workspace.models import Message, WorkspaceSession
from app.workspace.schemas import MessageCreate, SessionCreate, SessionUpdate


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


BLANK_SESSION_TITLE_PREFIX = "新会话"

_DEFAULT_TITLE_PATTERN = re.compile(r"^新会话\d+$")


def is_default_session_title(title: str) -> bool:
    """判断标题是否仍是空白会话的「新会话N」默认名。"""
    return bool(_DEFAULT_TITLE_PATTERN.fullmatch(title.strip()))


def default_session_title(brand: str, campaign_name: str | None, category: str | None) -> str | None:
    """从表单字段推导标题；全部为空时返回 None，由创建流程以「新会话N」兜底。"""
    normalized_brand = brand.strip()
    normalized_campaign = campaign_name.strip() if campaign_name else ""
    normalized_category = category.strip() if category else ""
    if normalized_brand and normalized_campaign:
        return f"{normalized_brand}-{normalized_campaign}"
    if normalized_brand:
        return normalized_brand
    if normalized_campaign:
        return normalized_campaign
    if normalized_category:
        return f"{normalized_category} KOL 分析"
    return None


class WorkspaceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_owned_session(
        self, user_id: str, session_id: str, *, for_update: bool = False
    ) -> WorkspaceSession:
        statement = select(WorkspaceSession).where(
            WorkspaceSession.id == session_id,
            WorkspaceSession.user_id == user_id,
            WorkspaceSession.deleted_at.is_(None),
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
            .where(
                WorkspaceSession.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
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
        title = default_session_title(payload.brand, payload.campaign_name, payload.category)
        if title is None:
            title = await self._next_blank_session_title(user_id)
        workspace = WorkspaceSession(
            id=str(uuid4()),
            user_id=user_id,
            title=title,
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
        if payload.initial_query is not None:
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

    async def _next_blank_session_title(self, user_id: str) -> str:
        """空白会话默认名「新会话N」：N = 该用户「新会话」开头标题的会话数 + 1。"""
        count = await self.db.scalar(
            select(func.count())
            .select_from(WorkspaceSession)
            .where(
                WorkspaceSession.user_id == user_id,
                WorkspaceSession.title.like(f"{BLANK_SESSION_TITLE_PREFIX}%"),
            )
        )
        return f"{BLANK_SESSION_TITLE_PREFIX}{(count or 0) + 1}"

    async def delete_session(self, user_id: str, session_id: str) -> None:
        workspace = await self.db.scalar(
            select(WorkspaceSession)
            .where(
                WorkspaceSession.id == session_id,
                WorkspaceSession.user_id == user_id,
            )
            .with_for_update()
        )
        if workspace is None:
            raise LookupError("session_not_found")
        if workspace.deleted_at is None:
            now = utc_now()
            workspace.deleted_at = now
            workspace.updated_at = now
            await self.db.flush()

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
            if value is None:
                continue
            # DTO 的 filters 对应 ORM 列 filters_snapshot。
            if field == "filters":
                workspace.filters_snapshot = value
            else:
                setattr(workspace, field, value)
        workspace.updated_at = utc_now()
        workspace.last_accessed_at = workspace.updated_at
        await self.db.flush()
        return workspace
