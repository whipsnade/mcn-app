from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select, text

from app.db.session import SessionFactory
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import FOLLOWUP_PROMPT
from app.reporting.models import BiReport
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.state import TaskEventType, TaskStatus
from app.tasks.repository import TaskRepository
from app.workspace.models import Message, WorkspaceSession


_INTERNAL_REFERENCE_RE = re.compile(
    r"(?:https?://|wss?://|/api/|\bbearer\b|\bsk-[a-z0-9_-]+\b|"
    r"\bdatatap\b|\bmcp\b|\bstep_[a-z0-9_-]+\b|"
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}\b)",
    re.IGNORECASE,
)


def contains_internal_reference(value: str) -> bool:
    return bool(_INTERNAL_REFERENCE_RE.search(value))


def _require_chinese(value: str) -> str:
    visible = [char for char in value if char.isalnum()]
    chinese = re.findall(r"[\u4e00-\u9fff]", value)
    if not visible or len(chinese) / len(visible) < 0.6:
        raise ValueError("chinese_text_required")
    if contains_internal_reference(value):
        raise ValueError("internal_reference_forbidden")
    return value.strip()


class FollowupSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=40)
    prompt: str = Field(min_length=5, max_length=500)
    rationale: str = Field(min_length=2, max_length=160)

    _title_chinese = field_validator("title")(_require_chinese)
    _prompt_chinese = field_validator("prompt")(_require_chinese)
    _rationale_chinese = field_validator("rationale")(_require_chinese)


class FollowupSuggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestions: tuple[FollowupSuggestion, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def unique_suggestions(self) -> "FollowupSuggestions":
        keys = {(item.title.strip(), item.prompt.strip()) for item in self.suggestions}
        if len(keys) != len(self.suggestions):
            raise ValueError("duplicate_suggestions")
        return self


def _safe_json(value: Any, *, max_length: int = 6_000) -> str:
    """Serialize only already-projected fields and cap prompt size."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded[:max_length]


def build_followup_request(
    *,
    user_query: str,
    session_filters: Mapping[str, Any],
    tool_summary: Mapping[str, Any],
    candidate_count: int,
    bi_summary: Mapping[str, Any],
    conclusion: str,
) -> StructuredModelRequest[FollowupSuggestions]:
    safe_input = {
        "用户问题": user_query[:5_000],
        "会话筛选条件": {
            key: session_filters.get(key)
            for key in ("brand", "campaign_name", "category", "target_audience", "platforms", "budget_min", "budget_max")
            if session_filters.get(key) is not None
        },
        "渠道响应概况": dict(tool_summary),
        "候选数量": max(0, min(int(candidate_count), 10_000)),
        "BI指标摘要": dict(bi_summary),
        "本轮结论": conclusion[:2_000],
    }
    return StructuredModelRequest(
        purpose="followup",
        template_name=FOLLOWUP_PROMPT.name,
        messages=(
            ChatMessage(role="system", content=FOLLOWUP_PROMPT.system),
            ChatMessage(role="user", content=_safe_json(safe_input)),
        ),
        output_model=FollowupSuggestions,
        max_tokens=2_048,
    )


def safe_followup_error(error: BaseException, *, stage: str) -> dict[str, Any]:
    """Return diagnostics that can never include raw model/MCP payloads."""
    if isinstance(error, ValidationError):
        fields = []
        for item in error.errors(include_url=False, include_input=False)[:20]:
            loc = item.get("loc", ())
            fields.append({
                "field": ".".join(str(part)[:64] for part in loc[:8]),
                "type": str(item.get("type", "validation_error"))[:64],
            })
        code = "FOLLOWUP_SCHEMA_INVALID"
    else:
        code = str(getattr(error, "code", "FOLLOWUP_GENERATION_FAILED"))[:64]
        fields = []
    return {
        "error_code": code,
        "stage": stage[:64],
        "fields": fields,
    }


class InMemoryFollowupLock:
    _locks: dict[str, asyncio.Lock] = {}
    _guard = asyncio.Lock()

    async def acquire(self, task_id: str, *, timeout: float | None = None) -> bool:
        async with self._guard:
            lock = self._locks.setdefault(task_id, asyncio.Lock())
        if timeout == 0 and lock.locked():
            return False
        try:
            if timeout is None:
                await lock.acquire()
            else:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def release(self, task_id: str) -> None:
        lock = self._locks.get(task_id)
        if lock is not None and lock.locked():
            lock.release()


class FollowupExecutionLock:
    """Process lock plus a MySQL advisory lock when the production DB supports it."""

    _memory = InMemoryFollowupLock()

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._db = None
        self._held = False

    async def __aenter__(self) -> bool:
        if not await self._memory.acquire(self.task_id, timeout=0):
            return False
        try:
            self._db = SessionFactory()
            bind = self._db.bind
            dialect = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect == "mysql":
                result = await self._db.scalar(
                    text("SELECT GET_LOCK(:lock_name, 0)"),
                    {"lock_name": f"kol_followup:{self.task_id}"},
                )
                if result != 1:
                    await self._db.close()
                    self._db = None
                    await self._memory.release(self.task_id)
                    return False
            self._held = True
            return True
        except Exception:
            if self._db is not None:
                await self._db.close()
                self._db = None
            self._held = True
            return True

    async def __aexit__(self, *_: Any) -> None:
        try:
            if self._db is not None:
                bind = self._db.bind
                dialect = getattr(getattr(bind, "dialect", None), "name", "")
                if dialect == "mysql":
                    await self._db.scalar(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": f"kol_followup:{self.task_id}"},
                    )
                await self._db.close()
        finally:
            if self._held:
                await self._memory.release(self.task_id)


async def _summary_message(db, task: AnalysisTask) -> Message | None:
    messages = list(
        (
            await db.scalars(
                select(Message)
                .where(
                    Message.session_id == task.session_id,
                    Message.user_id == task.user_id,
                    Message.role == "assistant",
                )
                .order_by(Message.sequence.desc())
                .with_for_update()
            )
        ).all()
    )
    return next((item for item in messages if item.metadata_json.get("task_id") == task.id), None)


class FollowupSuggestionService:
    def __init__(self, model: ModelAdapter, *, lock_factory=FollowupExecutionLock) -> None:
        self.model = model
        self.lock_factory = lock_factory

    async def prepare(self, task_id: str) -> bool:
        async with SessionFactory.begin() as db:
            task = await db.get(AnalysisTask, task_id, with_for_update=True)
            if task is None:
                return False
            message = await _summary_message(db, task)
            if message is None:
                return False
            metadata = dict(message.metadata_json or {})
            status = metadata.get("followup_suggestions_status")
            if status in {"pending", "completed", "failed"}:
                return status == "pending"
            message.metadata_json = {
                **metadata,
                "task_id": task.id,
                "followup_suggestions_status": "pending",
                "followup_suggestions": [],
                "followup_suggestions_started_at": _now().isoformat(),
            }
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.FOLLOWUP_SUGGESTIONS_STARTED,
                {"session_id": task.session_id, "task_id": task.id},
            )
            return True

    async def generate(self, task_id: str) -> bool:
        async with self.lock_factory(task_id) as acquired:
            if not acquired:
                return False
            try:
                snapshot = await self._input_snapshot(task_id)
            except Exception as error:
                await self._persist_failure(task_id, safe_followup_error(error, stage="prepare"))
                return False
            if snapshot is None:
                return False
            try:
                result = await self.model.complete_json(build_followup_request(**snapshot))
                suggestions = result.value.model_dump(mode="json")["suggestions"]
            except Exception as error:
                await self._persist_failure(task_id, safe_followup_error(error, stage="model"))
                return False
            await self._persist_success(task_id, suggestions)
            return True

    async def _input_snapshot(self, task_id: str) -> dict[str, Any] | None:
        async with SessionFactory() as db:
            task = await db.get(AnalysisTask, task_id)
            if task is None or task.status not in {
                TaskStatus.COMPLETED.value,
                TaskStatus.COMPLETED_WITH_WARNINGS.value,
            }:
                return None
            message = await _summary_message(db, task)
            if message is None or message.metadata_json.get("followup_suggestions_status") != "pending":
                return None
            user_message = await db.get(Message, task.trigger_message_id)
            session = await db.get(WorkspaceSession, task.session_id)
            report = await db.scalar(
                select(BiReport)
                .where(BiReport.task_id == task.id)
                .order_by(BiReport.report_version.desc())
            )
            events = list((await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))).all())
            tool_summary: dict[str, dict[str, int]] = {}
            for event in events:
                if event.event_type not in {
                    TaskEventType.TOOL_SUCCEEDED,
                    TaskEventType.TOOL_FAILED,
                    TaskEventType.TOOL_UNKNOWN,
                }:
                    continue
                platform = str(event.payload_json.get("platform", "未知渠道"))[:32]
                counts = tool_summary.setdefault(platform, {"succeeded": 0, "failed": 0, "unknown": 0})
                key = "succeeded" if event.event_type == TaskEventType.TOOL_SUCCEEDED else "failed" if event.event_type == TaskEventType.TOOL_FAILED else "unknown"
                counts[key] += 1
            chart = (report.chart_data_json if report is not None else {}) or {}
            overview = chart.get("overview", {}) if isinstance(chart, dict) else {}
            analytics = chart.get("analytics", {}) if isinstance(chart, dict) else {}
            bi_summary = {
                "overview": {
                    key: overview.get(key)
                    for key in ("candidate_count", "highest_score", "average_score", "platform_count")
                    if isinstance(overview, dict) and isinstance(overview.get(key), (int, float, str))
                },
                "analytics_available": sorted(analytics.keys()) if isinstance(analytics, dict) else [],
            }
            candidate_count = int((overview or {}).get("candidate_count") or 0) if isinstance(overview, dict) else 0
            return {
                "user_query": (user_message.content if user_message else "")[:5_000],
                "session_filters": {
                    "brand": session.brand if session else None,
                    "campaign_name": session.campaign_name if session else None,
                    "category": session.category if session else None,
                    "target_audience": session.target_audience if session else None,
                    "platforms": session.platforms if session else [],
                    "budget_min": str(session.budget_min) if session and session.budget_min is not None else None,
                    "budget_max": str(session.budget_max) if session and session.budget_max is not None else None,
                },
                "tool_summary": tool_summary,
                "candidate_count": candidate_count,
                "bi_summary": bi_summary,
                "conclusion": (report.conclusion_text if report else "")[:2_000],
            }

    async def _persist_success(self, task_id: str, suggestions: list[dict[str, Any]]) -> None:
        async with SessionFactory.begin() as db:
            task = await db.get(AnalysisTask, task_id, with_for_update=True)
            if task is None:
                return
            message = await _summary_message(db, task)
            if message is None or message.metadata_json.get("followup_suggestions_status") != "pending":
                return
            message.metadata_json = {
                **message.metadata_json,
                "followup_suggestions_status": "completed",
                "followup_suggestions": suggestions[:5],
                "followup_suggestions_generated_at": _now().isoformat(),
            }
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.FOLLOWUP_SUGGESTIONS_UPDATED,
                {"session_id": task.session_id, "task_id": task.id, "count": len(suggestions[:5])},
            )

    async def _persist_failure(self, task_id: str, error: dict[str, Any]) -> None:
        async with SessionFactory.begin() as db:
            task = await db.get(AnalysisTask, task_id, with_for_update=True)
            if task is None:
                return
            message = await _summary_message(db, task)
            if message is None or message.metadata_json.get("followup_suggestions_status") != "pending":
                return
            message.metadata_json = {
                **message.metadata_json,
                "followup_suggestions_status": "failed",
                "followup_suggestions": [],
                "followup_error": error,
            }
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.FOLLOWUP_SUGGESTIONS_FAILED,
                {"session_id": task.session_id, "task_id": task.id, **error},
            )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
