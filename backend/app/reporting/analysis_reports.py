"""自由分析报告（agent 任务产物）的构建与查询。

与 BI 报告不同：没有候选版本概念，报告内容来自 report_writer 模型对
已结算 MCP 证据的结构化撰写，落库前经 ReportDocument 再次校验。
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.models import McpCall
from app.orchestration.context import project_reporting_summary
from app.reporting.blocks import ReportDocument
from app.reporting.models import AnalysisReport
from app.tasks.models import AnalysisTask
from app.tasks.repository import TaskRepository
from app.tasks.state import TaskEventType
from app.workspace.models import WorkspaceSession


# 单条证据进入报告撰写模型前的长度上限；超出部分截断，不进入模型上下文。
_MAX_EVIDENCE_CHARS = 6_000


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def sanitize_evidence(payload: Any) -> Any:
    """复用 reporting 投影剔除端点/密钥/URL，再按长度截断。"""
    projected = project_reporting_summary({"data": payload}).get("data")
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= _MAX_EVIDENCE_CHARS:
        return projected
    return encoded[:_MAX_EVIDENCE_CHARS] + "…(truncated)"


class AnalysisReportService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def writer_input(self, task_id: str) -> dict[str, Any]:
        """report_writer 的输入：已结算调用的脱敏证据，按调用顺序排列。"""
        task = await self._db.get(AnalysisTask, task_id)
        if task is None:
            raise LookupError("analysis_task_not_found")
        rows = await self._settled_calls(task_id)
        return {
            "task_id": task_id,
            "evidence": [
                {
                    "step": row.plan_step_id,
                    "tool": row.internal_tool_name,
                    "data": sanitize_evidence((row.evidence_json or {}).get("structured_content")),
                }
                for row in rows
            ],
        }

    async def build(
        self,
        task_id: str,
        *,
        document: ReportDocument,
        lease_owner: str | None = None,
    ) -> AnalysisReport:
        """持久化一份版本化自由报告；重复进入返回既有报告（幂等）。"""
        task = await self._db.scalar(
            select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
        )
        if task is None:
            raise LookupError("analysis_task_not_found")
        self._require_active_lease(task, lease_owner)
        existing = await self._db.scalar(
            select(AnalysisReport)
            .where(AnalysisReport.task_id == task.id, AnalysisReport.status == "completed")
            .order_by(AnalysisReport.version.desc())
        )
        if existing is not None:
            return existing
        # version 按会话编号（迁移 0020 起 (session_id, version) 唯一），
        # 同一会话多个任务的报告依次递增。
        version = (
            await self._db.scalar(
                select(func.max(AnalysisReport.version)).where(
                    AnalysisReport.session_id == task.session_id
                )
            )
            or 0
        ) + 1
        now = _now()
        report = AnalysisReport(
            id=str(uuid4()),
            task_id=task.id,
            session_id=task.session_id,
            version=version,
            title=document.title,
            blocks_json=[block.model_dump(mode="json") for block in document.blocks],
            conclusion_text=document.conclusion,
            status="completed",
            created_at=now,
            updated_at=now,
        )
        self._db.add(report)
        await self._db.flush()
        await TaskRepository(self._db).append_event(
            task.id,
            task.user_id,
            TaskEventType.REPORT_UPDATED,
            {
                "report_id": report.id,
                "version": version,
                "phase": "ai_summary",
                "label": "分析报告已生成",
            },
        )
        return report

    async def build_session_report(
        self,
        *,
        user_id: str,
        session_id: str,
        document: ReportDocument,
    ) -> AnalysisReport:
        """持久化一份会话级自由报告（task_id 为 NULL）；不幂等，每次调用生成新版本。

        同步端点直接返回报告，不发 report.updated 事件。同会话并发点击可能
        撞 (session_id, version) 唯一约束：SAVEPOINT 回滚后重算 version 重试
        一次，再失败抛领域错误由端点层映射。

        错误契约：
        - ``LookupError("session_not_found")``：会话不存在/不属于该用户/已软删 → 404；
        - ``LookupError("report_version_conflict")``：两次写入均撞唯一约束 → 409。
        """
        session = await self._db.scalar(
            select(WorkspaceSession).where(
                WorkspaceSession.id == session_id,
                WorkspaceSession.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
        )
        if session is None:
            raise LookupError("session_not_found")
        for attempt in (1, 2):
            version = (
                await self._db.scalar(
                    select(func.max(AnalysisReport.version)).where(
                        AnalysisReport.session_id == session_id
                    )
                )
                or 0
            ) + 1
            now = _now()
            report = AnalysisReport(
                id=str(uuid4()),
                task_id=None,
                session_id=session_id,
                version=version,
                title=document.title,
                blocks_json=[block.model_dump(mode="json") for block in document.blocks],
                conclusion_text=document.conclusion,
                status="completed",
                created_at=now,
                updated_at=now,
            )
            try:
                async with self._db.begin_nested():
                    self._db.add(report)
                    await self._db.flush()
                return report
            except IntegrityError:
                # 宽捕获是有意的：FK 违规等其他冲突重试一次无害；第二次仍失败
                # 说明并发写入持续竞争，抛领域错误交给端点层映射 409。
                if attempt == 2:
                    raise LookupError("report_version_conflict") from None
        raise RuntimeError("unreachable")  # pragma: no cover

    async def get_owned_report(self, user_id: str, report_id: str) -> AnalysisReport:
        # 按会话归属鉴权（session_id 全行 NOT NULL），任务级与会话级报告统一。
        report = await self._db.scalar(
            select(AnalysisReport)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisReport.session_id)
            .where(
                AnalysisReport.id == report_id,
                WorkspaceSession.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
        )
        if report is None:
            raise LookupError("report_not_found")
        return report

    async def latest_session_report(self, session_id: str) -> AnalysisReport | None:
        # version 按会话递增（迁移 0020），比 created_at 排序更确定
        # （MySQL DATETIME 秒级精度，同秒两次构建会并列）。
        return await self._db.scalar(
            select(AnalysisReport)
            .where(AnalysisReport.session_id == session_id, AnalysisReport.status == "completed")
            .order_by(AnalysisReport.version.desc())
            .limit(1)
        )

    async def _settled_calls(self, task_id: str) -> list[McpCall]:
        return list(
            (
                await self._db.scalars(
                    select(McpCall)
                    .where(
                        McpCall.task_id == task_id,
                        McpCall.status == McpCallStatus.SETTLED.value,
                    )
                    .order_by(McpCall.created_at.asc())
                )
            ).all()
        )

    @staticmethod
    def _require_active_lease(task: AnalysisTask, lease_owner: str | None) -> None:
        if lease_owner is None:
            return
        if (
            task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or task.lease_expires_at <= _now()
        ):
            raise RuntimeError("task_lease_lost")


__all__ = ["AnalysisReportService", "sanitize_evidence"]
