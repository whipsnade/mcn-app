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


# artifact_type → planner 摘要模块键（与 artifacts/router 的模块映射一致）。
_MODULE_KEY_BY_ARTIFACT_TYPE = {
    "brand_report": "brand",
    "campaign_report": "campaign",
    "kol_report": "kol_analysis",
    "kol_selection_set": "kol_selection",
}
_MODULE_ORDER = ("brand", "campaign", "kol_analysis", "kol_selection")


def _compact_scope(scope: Any) -> dict[str, Any] | None:
    """scope 紧凑投影：只保留短字段，防止 planner payload 膨胀。"""
    if not isinstance(scope, dict):
        return None
    compact = {
        key: scope[key]
        for key in ("brand", "campaign", "period", "platforms")
        if scope.get(key) is not None
    }
    return compact or None


async def recent_task_outcomes(
    db, user_id: str, session_id: str, *, limit: int = 3
) -> list[dict[str, Any]]:
    """最近任务终态投影：planner 判断失败追问的事实依据，也是答疑证据包素材。"""
    rows = list(
        (
            await db.scalars(
                select(AnalysisTask)
                .where(
                    AnalysisTask.user_id == user_id,
                    AnalysisTask.session_id == session_id,
                )
                .order_by(AnalysisTask.created_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return [
        {
            "status": task.status,
            "error_code": task.error_code,
            "error_message": task.error_message,
            "completed_at": (
                task.completed_at.isoformat() if task.completed_at else None
            ),
        }
        for task in rows
    ]


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
    # enforce 入口注入的已审核 MCP 工具紧凑投影（internal_name/description/required_params）；
    # 影子入口不注入，保持空 tuple。
    available_tools: tuple[dict[str, Any], ...] = ()


class GoalPlannerContextBuilder:
    def __init__(self, session_factory=SessionFactory) -> None:
        self._session_factory = session_factory

    async def build(self, task_id: str) -> GoalPlannerContext:
        async with self._session_factory() as db:
            return await self._build(db, task_id)

    async def build_for_message(
        self,
        user_id: str,
        session_id: str,
        message: str,
        *,
        db=None,
        available_tools: tuple[dict[str, Any], ...] = (),
    ) -> GoalPlannerContext:
        """不依赖 task 的规划上下文（enforce 入口：任务尚未创建）。

        current_message 用入参（此刻用户消息可能尚未落库）；recent_messages
        取会话最近消息（sequence 正序）并在尾部补上这条新消息。
        available_tools 是已审核 MCP 工具的紧凑投影，供 planner 围绕数据能力追问。
        """
        if db is not None:
            return await self._build_for_message(
                db, user_id, session_id, message, available_tools=available_tools
            )
        async with self._session_factory() as owned_db:
            return await self._build_for_message(
                owned_db, user_id, session_id, message, available_tools=available_tools
            )

    async def _build_for_message(
        self,
        db,
        user_id: str,
        session_id: str,
        message: str,
        *,
        available_tools: tuple[dict[str, Any], ...] = (),
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
        session_context, account_default_brand, exemplars, artifact_summaries = (
            await self._session_parts(db, user_id=user_id, workspace=workspace)
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
            artifact_summaries=artifact_summaries,
            exemplars=exemplars,
            available_tools=available_tools,
        )

    async def _session_parts(self, db, *, user_id: str, workspace) -> tuple:
        """session_context / account_default_brand / exemplars / artifact_summaries 公共组装。"""
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
            "recent_task_outcomes": await recent_task_outcomes(
                db, user_id, workspace.id
            ),
        }
        artifact_summaries = await self._artifact_summaries(db, workspace.id)
        return session_context, account_default_brand, tuple(exemplars), artifact_summaries

    async def _artifact_summaries(self, db, session_id: str) -> tuple[dict[str, Any], ...]:
        """每 module 最新一条 completed artifact 的紧凑投影（设计 §6.1 planner 输入）。"""
        # 延迟导入：artifacts.models 依赖 goals 包，模块级导入会循环。
        from app.artifacts.models import TaskArtifact

        rows = list(
            (
                await db.scalars(
                    select(TaskArtifact)
                    .where(
                        TaskArtifact.session_id == session_id,
                        TaskArtifact.status == "completed",
                    )
                    .order_by(TaskArtifact.version.desc(), TaskArtifact.created_at.desc())
                )
            ).all()
        )
        latest_by_module: dict[str, TaskArtifact] = {}
        for row in rows:
            module_key = _MODULE_KEY_BY_ARTIFACT_TYPE.get(row.artifact_type)
            if module_key is not None and module_key not in latest_by_module:
                latest_by_module[module_key] = row
        summaries: list[dict[str, Any]] = []
        for module_key in _MODULE_ORDER:
            row = latest_by_module.get(module_key)
            if row is None:
                continue
            summaries.append(
                {
                    "module_key": module_key,
                    "artifact_type": row.artifact_type,
                    "title": row.title[:80],
                    "version": row.version,
                    "scope": _compact_scope(row.scope_json),
                    "created_at": row.created_at.isoformat(),
                }
            )
        return tuple(summaries)

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
        session_context, account_default_brand, exemplars, artifact_summaries = (
            await self._session_parts(db, user_id=task.user_id, workspace=workspace)
        )
        return GoalPlannerContext(
            user_id=task.user_id,
            session_id=task.session_id,
            task_id=task.id,
            current_message=trigger.content,
            recent_messages=compress_messages(messages, max_chars=12_000),
            session_context=session_context,
            account_default_brand=account_default_brand,
            artifact_summaries=artifact_summaries,
            exemplars=exemplars,
        )
