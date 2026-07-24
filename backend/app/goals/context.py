from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db.session import SessionFactory
from app.identity.models import UserBrandProfile
from app.model.exemplars import find_success_exemplars
from app.orchestration.context import compress_messages
from app.orchestration.schemas import PlannerMessage
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


@dataclass(frozen=True)
class GoalPlannerContext:
    user_id: str
    session_id: str
    task_id: str
    current_message: str
    recent_messages: tuple[PlannerMessage, ...]
    session_context: dict[str, Any]
    account_default_brand: str | None
    artifact_summaries: tuple[dict[str, Any], ...]
    exemplars: tuple[dict[str, Any], ...] = ()
    allowed_goal_types: tuple[str, ...] = (
        "brand_analysis",
        "campaign_analysis",
        "kol_selection",
    )


class GoalPlannerContextBuilder:
    def __init__(self, session_factory=SessionFactory) -> None:
        self._session_factory = session_factory

    async def build(self, task_id: str) -> GoalPlannerContext:
        async with self._session_factory() as db:
            return await self._build(db, task_id)

    async def build_for_message(
        self, user_id: str, session_id: str, message: str, *, db=None
    ) -> GoalPlannerContext:
        """不依赖 task 的规划上下文（enforce 入口：任务尚未创建）。

        current_message 用入参（此刻用户消息可能尚未落库）；recent_messages
        取会话最近消息（sequence 正序）并在尾部补上这条新消息。
        """
        if db is not None:
            return await self._build_for_message(db, user_id, session_id, message)
        async with self._session_factory() as owned_db:
            return await self._build_for_message(owned_db, user_id, session_id, message)

    async def _build_for_message(
        self, db, user_id: str, session_id: str, message: str
    ) -> GoalPlannerContext:
        workspace = await db.scalar(
            select(WorkspaceSession).where(
                WorkspaceSession.id == session_id,
                WorkspaceSession.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
        )
        if workspace is None:
            raise LookupError("session_not_found")
        history = list(
            (
                await db.scalars(
                    select(Message)
                    .where(
                        Message.session_id == session_id,
                        Message.user_id == user_id,
                    )
                    .order_by(Message.sequence.desc())
                    .limit(20)
                )
            ).all()
        )
        history.reverse()
        recent = list(compress_messages(history, max_chars=12_000))
        if recent:
            tail_sequence = recent[-1].sequence + 1
        else:
            tail_sequence = 1
        recent.append(
            PlannerMessage(role="user", content=message, sequence=tail_sequence)
        )
        session_context, account_default_brand, exemplars = await self._session_parts(
            db, user_id=user_id, workspace=workspace
        )
        return GoalPlannerContext(
            user_id=user_id,
            session_id=session_id,
            # 任务尚未创建：task_id 置空串（仅用于日志上下文）。
            task_id="",
            current_message=message,
            recent_messages=tuple(recent),
            session_context=session_context,
            account_default_brand=account_default_brand,
            artifact_summaries=(),
            exemplars=exemplars,
        )

    async def _session_parts(self, db, *, user_id: str, workspace) -> tuple:
        """session_context / account_default_brand / exemplars 公共组装。"""
        profile = (workspace.filters_snapshot or {}).get("brainstorm_profile") or {}
        active_brand = workspace.brand or profile.get("brand") or None
        # 账户级默认品牌：来自 user_brand_profiles（品牌解析优先级最低档）。
        account_default_brand = await db.scalar(
            select(UserBrandProfile.brand_name).where(
                UserBrandProfile.user_id == user_id,
                UserBrandProfile.is_default.is_(True),
            )
        )
        exemplars = await find_success_exemplars(
            db,
            purpose="goal_planner",
            tags=["goal_planner:shadow"],
            user_id=user_id,
        )
        session_context = {
            "active_brand": active_brand,
            "campaign_name": workspace.campaign_name,
            "category": workspace.category,
            "platforms": list(workspace.platforms or []),
            "target_audience": workspace.target_audience,
            "brainstorm_profile": profile,
        }
        return session_context, account_default_brand, tuple(exemplars)

    async def _build(self, db, task_id: str) -> GoalPlannerContext:
        task = await db.get(AnalysisTask, task_id)
        if task is None:
            raise LookupError("analysis_task_not_found")
        workspace = await db.scalar(
            select(WorkspaceSession).where(
                WorkspaceSession.id == task.session_id,
                WorkspaceSession.user_id == task.user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
        )
        if workspace is None:
            raise LookupError("session_not_found")
        trigger = await db.scalar(
            select(Message).where(
                Message.id == task.trigger_message_id,
                Message.session_id == task.session_id,
                Message.user_id == task.user_id,
            )
        )
        if trigger is None:
            raise LookupError("trigger_message_not_found")
        messages = list(
            (
                await db.scalars(
                    select(Message)
                    .where(
                        Message.session_id == task.session_id,
                        Message.user_id == task.user_id,
                        Message.sequence <= trigger.sequence,
                    )
                    .order_by(Message.sequence)
                )
            ).all()
        )
        session_context, account_default_brand, exemplars = await self._session_parts(
            db, user_id=task.user_id, workspace=workspace
        )
        return GoalPlannerContext(
            user_id=task.user_id,
            session_id=task.session_id,
            task_id=task.id,
            current_message=trigger.content,
            recent_messages=compress_messages(messages, max_chars=12_000),
            session_context=session_context,
            account_default_brand=account_default_brand,
            artifact_summaries=(),
            exemplars=exemplars,
        )
