"""Pi POC Evidence 旁路服务：start/settle/fail 状态机，零积分。

只处理 Pi 工具调用的可审计旁路：start 落 AgentStep + AgentToolCall（零积分、
logical_call_id = run_id + Pi call id，不依赖参数 hash 熔断），settle 写不可变
EvidenceItem，fail/unknown 不产生 available Evidence。不接入 DataTap、Producer、
正式 Runtime 或积分。
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import ArtifactDraft, ArtifactDraftRevision
from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.tools.contracts import ToolResult
from app.agent_runtime.tools.registry import UnknownToolError
from app.core.config import Settings
from app.pi_runtime_poc.auth import verify_run_token
from app.pi_runtime_poc.internal_tools import (
    PIPOC_PROFILE,
    build_pi_internal_registry,
)
from app.pi_runtime_poc.schemas import (
    PiToolFailed,
    PiToolSettled,
    PiToolSettledResponse,
    PiToolStarted,
    PiToolStartedResponse,
)

_SERVICE_NAME = "pi_poc_datatap"
_STEP_TYPE = "tool_call"
_BUILDER_TOOL_NAMES = frozenset(
    {
        "build_brand_report_draft",
        "build_campaign_report_draft",
        "build_kol_selection_draft",
        "build_kol_analysis_draft",
        "build_kol_detail_draft",
        "build_insight_draft",
    }
)
_MAX_FEEDBACK_SECTIONS = 50
_MAX_FEEDBACK_REFS = 100
_MAX_FEEDBACK_SOURCES_PER_REF = 20
_MAX_FEEDBACK_REASON_CODES = 20
_MAX_FEEDBACK_TEXT = 500


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _logical_call_id(run_id: str, pi_call_id: str) -> str:
    """logical_call_id = run + Pi call id；不把参数 hash 当业务熔断键。"""
    return hashlib.sha256(f"{run_id}\x00{pi_call_id}".encode()).hexdigest()


def _arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _bounded_text(value: Any) -> str:
    return str(value)[:_MAX_FEEDBACK_TEXT]


def _feedback_availability(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """提取可安全回灌的 availability 与章节 coverage，不返回正式 payload。"""
    raw = payload.get("availability")
    if not isinstance(raw, dict):
        return {}, {"complete_sections": [], "restricted_sections": []}

    availability: dict[str, Any] = {}
    complete_sections: list[str] = []
    restricted_sections: list[str] = []
    for section, entry in list(raw.items())[:_MAX_FEEDBACK_SECTIONS]:
        if not isinstance(section, str) or not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if not isinstance(status, str):
            continue
        reason_codes = entry.get("reason_codes")
        safe_reason_codes = (
            [_bounded_text(code) for code in reason_codes[:_MAX_FEEDBACK_REASON_CODES]]
            if isinstance(reason_codes, list)
            else []
        )
        availability[section] = {"status": status, "reason_codes": safe_reason_codes}
        if status == "complete":
            complete_sections.append(section)
        else:
            restricted_sections.append(section)
    return availability, {
        "complete_sections": complete_sections,
        "restricted_sections": restricted_sections,
    }


def _feedback_evidence_refs(refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """保留 Evidence lineage 的定位信息，限制数量且绝不回传原始 Evidence。"""
    projected: list[dict[str, Any]] = []
    for reference in (refs or [])[:_MAX_FEEDBACK_REFS]:
        if not isinstance(reference, dict):
            continue
        artifact_path = reference.get("artifact_path")
        raw_sources = reference.get("sources")
        if not isinstance(artifact_path, str) or not isinstance(raw_sources, list):
            continue
        sources: list[dict[str, str]] = []
        for source in raw_sources[:_MAX_FEEDBACK_SOURCES_PER_REF]:
            if not isinstance(source, dict):
                continue
            evidence_id = source.get("evidence_id")
            source_path = source.get("source_path")
            if isinstance(evidence_id, str) and isinstance(source_path, str):
                sources.append({"evidence_id": evidence_id, "source_path": source_path})
        if sources:
            projected.append({"artifact_path": artifact_path, "sources": sources})
    return projected


def _feedback_limitations(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("limitations")
    if not isinstance(raw, list):
        return []
    projected: list[dict[str, str]] = []
    for limitation in raw[:_MAX_FEEDBACK_SECTIONS]:
        if not isinstance(limitation, dict):
            continue
        code = limitation.get("code")
        message = limitation.get("message")
        if isinstance(code, str) or isinstance(message, str):
            projected.append(
                {
                    "code": _bounded_text(code) if code is not None else "",
                    "message": _bounded_text(message) if message is not None else "",
                }
            )
    return projected


class PiEvidenceIngestService:
    """零积分 Evidence 旁路状态机 + 受控内部工具桥（仅 POC 内部使用）。"""

    def __init__(
        self,
        *,
        db: AsyncSession,
        events: AgentEventStream,
        settings: Settings,
        worker_id: str = "pi-poc",
    ) -> None:
        self._db = db
        self._events = events
        self._settings = settings
        self._worker_id = worker_id

    async def execute_internal_tool(
        self, *, token: str, run_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """执行白名单内部工具；身份一律来自 token 对应 Run，伪造字段被 Registry 剥离。

        publish_artifacts 前确保当前 worker 持有 Run 活跃租约（发布前置条件）。
        """
        verify_run_token(token, run_id, settings=self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found")
        registry = build_pi_internal_registry(db=self._db, worker_id=self._worker_id)
        if tool_name == "publish_artifacts":
            repo = AgentRunRepository(self._db)
            if not await repo.holds_lease(run_id, self._worker_id):
                raise HTTPException(status.HTTP_409_CONFLICT, "pi_run_lease_not_held")
        try:
            result = await registry.execute(
                internal_name=tool_name,
                arguments=arguments,
                user_id=run.user_id,
                session_id=run.session_id,
                run_id=run.id,
                profile=PIPOC_PROFILE,
                channel_permissions=(),
            )
        except UnknownToolError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_internal_tool_not_found")
        if tool_name in _BUILDER_TOOL_NAMES and result.status == "success":
            result = await self._project_builder_feedback(run, result)
        return result.model_dump(mode="json")

    async def _project_builder_feedback(self, run: AgentRun, result: ToolResult) -> ToolResult:
        """从当前 Draft Revision 投影 Pi 补查所需的最小反馈。"""
        try:
            summary = json.loads(result.safe_summary)
        except json.JSONDecodeError:
            return result
        if not isinstance(summary, dict) or not isinstance(summary.get("draft_id"), str):
            return result

        draft = await self._db.get(ArtifactDraft, summary["draft_id"])
        if (
            draft is None
            or draft.owner_run_id != run.id
            or draft.session_id != run.session_id
        ):
            return result
        revision = await self._db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
                ArtifactDraftRevision.run_id == run.id,
            )
        )
        if revision is None or not isinstance(revision.payload_json, dict):
            return result

        availability, coverage = _feedback_availability(revision.payload_json)
        projected = {
            "artifact_id": summary.get("artifact_id"),
            "artifact_key": summary.get("artifact_key"),
            "draft_id": draft.id,
            "revision_id": revision.id,
            "revision": revision.revision,
            "schema_version": revision.schema_version,
            "data_status": revision.payload_json.get("data_status"),
            "availability": availability,
            "coverage": coverage,
            "limitations": _feedback_limitations(revision.payload_json),
            "evidence_refs": _feedback_evidence_refs(revision.evidence_refs_json),
        }
        return result.model_copy(
            update={"safe_summary": json.dumps(projected, ensure_ascii=False)}
        )

    async def start_tool(
        self, *, token: str, run_id: str, request: PiToolStarted
    ) -> PiToolStartedResponse:
        verify_run_token(token, run_id, settings=self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found")

        logical_call_id = _logical_call_id(run_id, request.call_id)
        existing = await self._db.scalar(
            select(AgentToolCall).where(AgentToolCall.logical_call_id == logical_call_id)
        )
        if existing is not None:
            return PiToolStartedResponse(call_id=existing.id)

        attempt = await self._ensure_attempt(run.id)
        step_sequence = await self._next_step_sequence(run.id)
        step = AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt.id,
            sequence=step_sequence,
            step_type=_STEP_TYPE,
            input_json={"internal_tool_name": request.tool_name, "arguments": request.arguments},
            status="running",
            visibility=run.visibility,
            created_at=_now(),
        )
        self._db.add(step)
        await self._db.flush()

        call = AgentToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            logical_call_id=logical_call_id,
            service=_SERVICE_NAME,
            internal_tool_name=request.tool_name,
            arguments_json=request.arguments,
            arguments_hash=_arguments_hash(request.arguments),
            status="running",
            points_reserved=0,
            points_settled=0,
        )
        self._db.add(call)
        await self._db.flush()

        await self._events.append(
            run.id,
            run.user_id,
            "tool.started",
            {"tool_name": request.tool_name, "call_id": call.id},
        )
        return PiToolStartedResponse(call_id=call.id)

    async def settle_tool(
        self, *, token: str, run_id: str, call_id: str, request: PiToolSettled
    ) -> PiToolSettledResponse:
        verify_run_token(token, run_id, settings=self._settings)
        call = await self._require_owned_call(run_id, call_id)

        if call.status == "settled":
            evidence = await self._db.scalar(
                select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
            )
            return PiToolSettledResponse(evidence_id=evidence.id if evidence else None)
        if call.status not in ("running", "planned"):
            raise HTTPException(status.HTTP_409_CONFLICT, "pi_tool_call_not_settleable")

        run = await self._db.get(AgentRun, run_id)
        assert run is not None
        writer = EvidenceWriter(self._db)
        evidence = await writer.write(
            session_id=run.session_id,
            run_id=run.id,
            tool_call_id=call.id,
            source_type="datatap_mcp",
            source_name=call.internal_tool_name,
            scope_json=None,
            period_json=None,
            raw_payload=request.raw_payload,
            availability_status="available",
        )
        call.status = "settled"
        call.completed_at = _now()
        await self._db.flush()
        await self._events.append(
            run.id,
            run.user_id,
            "tool.succeeded",
            {"tool_name": call.internal_tool_name, "call_id": call.id, "evidence_id": evidence.id},
        )
        return PiToolSettledResponse(evidence_id=evidence.id)

    async def fail_tool(
        self, *, token: str, run_id: str, call_id: str, request: PiToolFailed
    ) -> None:
        verify_run_token(token, run_id, settings=self._settings)
        call = await self._require_owned_call(run_id, call_id)
        if call.status in ("settled", "failed", "unknown"):
            return
        run = await self._db.get(AgentRun, run_id)
        assert run is not None
        call.status = request.status
        call.error_type = f"pi_poc_{request.status}"
        call.safe_error_message = (
            json.dumps(request.error, ensure_ascii=False, default=str)
            if request.error is not None
            else None
        )
        call.completed_at = _now()
        await self._db.flush()
        event_type = "tool.failed" if request.status == "failed" else "tool.unknown"
        await self._events.append(
            run.id,
            run.user_id,
            event_type,
            {"tool_name": call.internal_tool_name, "call_id": call.id},
        )

    async def _require_owned_call(self, run_id: str, call_id: str) -> AgentToolCall:
        call = await self._db.get(AgentToolCall, call_id)
        if call is None or call.run_id != run_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_tool_call_not_found")
        return call

    async def _ensure_attempt(self, run_id: str) -> AgentRunAttempt:
        attempt = await self._db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.run_id == run_id)
            .order_by(AgentRunAttempt.attempt.desc())
            .limit(1)
        )
        if attempt is not None:
            return attempt
        attempt = AgentRunAttempt(
            id=str(uuid4()),
            run_id=run_id,
            attempt=1,
            started_at=_now(),
            decision_count=0,
            outcome="running",
        )
        self._db.add(attempt)
        await self._db.flush()
        return attempt

    async def _next_step_sequence(self, run_id: str) -> int:
        max_sequence = await self._db.scalar(
            select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run_id)
        )
        return (max_sequence or 0) + 1
