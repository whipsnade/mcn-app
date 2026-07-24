from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db.session import SessionFactory
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
        profile = (workspace.filters_snapshot or {}).get("brainstorm_profile") or {}
        active_brand = workspace.brand or profile.get("brand") or None
        exemplars = await find_success_exemplars(
            db,
            purpose="goal_planner",
            tags=["goal_planner:shadow"],
            user_id=task.user_id,
        )
        return GoalPlannerContext(
            user_id=task.user_id,
            session_id=task.session_id,
            task_id=task.id,
            current_message=trigger.content,
            recent_messages=compress_messages(messages, max_chars=12_000),
            session_context={
                "active_brand": active_brand,
                "campaign_name": workspace.campaign_name,
                "category": workspace.category,
                "platforms": list(workspace.platforms or []),
                "target_audience": workspace.target_audience,
                "brainstorm_profile": profile,
            },
            account_default_brand=None,
            artifact_summaries=(),
            exemplars=tuple(exemplars),
        )
