"""Pi POC Evidence 旁路服务：start/settle/fail 状态机，零积分。

只处理 Pi 工具调用的可审计旁路：start 落 AgentStep + AgentToolCall（零积分、
logical_call_id = run_id + Pi call id，不依赖参数 hash 熔断），settle 写不可变
EvidenceItem，fail/unknown 不产生 available Evidence。不接入 DataTap、Producer、
正式 Runtime 或积分。
"""

import json
import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import ArtifactDraft, ArtifactDraftRevision
from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.models import AgentRun
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.contracts import ToolResult
from app.agent_runtime.tools.registry import UnknownToolError
from app.core.config import Settings
from app.pi_runtime_poc.audit import PiRunAuditWriter
from app.pi_runtime_poc.auth import verify_run_token
from app.pi_runtime_poc.diagnostics import safe_db_diagnostic
from app.pi_runtime_poc.internal_tools import (
    PIPOC_PROFILE,
    build_pi_internal_registry,
)
from app.pi_runtime_poc.schemas import (
    PiExtensionDiagnostic,
    PiSmokeRunFailed,
    PiToolFailed,
    PiToolSettled,
    PiToolSettledResponse,
    PiToolStarted,
    PiToolStartedResponse,
)

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
_LEASE_GATED_TOOL_NAMES = frozenset({"publish_artifacts", "request_clarification"})
_MAX_FEEDBACK_SECTIONS = 50
_MAX_FEEDBACK_REFS = 100
_MAX_FEEDBACK_SOURCES_PER_REF = 20
_MAX_FEEDBACK_REASON_CODES = 20
_MAX_FEEDBACK_TEXT = 500
_diagnostic_logger = logging.getLogger("pi_runtime_poc.diagnostics")


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

        发布或澄清前确保当前 worker 持有 Run 活跃租约。
        """
        verify_run_token(token, run_id, settings=self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found")
        registry = build_pi_internal_registry(db=self._db, worker_id=self._worker_id)
        if tool_name in _LEASE_GATED_TOOL_NAMES:
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
        if tool_name == "request_clarification" and result.status == "success":
            await self._events.append(
                run.id,
                run.user_id,
                "message.completed",
                {"type": "clarification"},
            )
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
        try:
            call_id = await PiRunAuditWriter(db=self._db, events=self._events).start_tool(
                run_id=run_id,
                pi_call_id=request.call_id,
                tool_name=request.tool_name,
                requested_tool_name=request.requested_tool_name,
                service_name=request.service_name,
                arguments=request.arguments,
            )
        except LookupError as error:
            if str(error) != "pi_run_not_found":
                raise
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found") from error
        except SQLAlchemyError as error:
            await self._db.rollback()
            _diagnostic_logger.warning(
                json.dumps(
                    {"stage": "audit_start", **safe_db_diagnostic(error)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "pi_poc_audit_start_failed",
            ) from None
        return PiToolStartedResponse(call_id=call_id)

    async def record_extension_diagnostic(
        self, *, token: str, run_id: str, diagnostic: PiExtensionDiagnostic
    ) -> None:
        """仅落安全阶段字段，供 Node Extension 在启动/注册失败时定位边界。"""
        verify_run_token(token, run_id, settings=self._settings)
        try:
            await PiRunAuditWriter(db=self._db, events=self._events).write_extension_diagnostic(
                run_id=run_id,
                diagnostic=diagnostic.model_dump(),
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found") from error

    async def fail_single_tool_smoke(
        self, *, token: str, run_id: str, request: PiSmokeRunFailed
    ) -> None:
        """只允许受控单工具冒烟 Run 在异常时自行收口，避免残留 running/queued。"""
        verify_run_token(token, run_id, settings=self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found")
        snapshot = run.prompt_snapshot_json or {}
        poc_snapshot = snapshot.get("pi_runtime_poc") if isinstance(snapshot, dict) else None
        if not isinstance(poc_snapshot, dict) or poc_snapshot.get("round_id") != "single-datatap-smoke":
            raise HTTPException(status.HTTP_409_CONFLICT, "pi_smoke_run_not_allowed")
        event = await self._events.settle_terminal(
            run.id,
            run.user_id,
            RunStatus.FAILED,
            {"error_code": request.code},
            worker_id="pi-poc-smoke",
        )
        if event is None and run.status not in (RunStatus.FAILED.value,):
            raise HTTPException(status.HTTP_409_CONFLICT, "pi_smoke_run_not_owned")

    async def complete_single_tool_smoke(self, *, token: str, run_id: str) -> None:
        """只允许受控单工具冒烟 Run 在成功后收口，避免遗留 queued/running。"""
        verify_run_token(token, run_id, settings=self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found")
        snapshot = run.prompt_snapshot_json or {}
        poc_snapshot = snapshot.get("pi_runtime_poc") if isinstance(snapshot, dict) else None
        if not isinstance(poc_snapshot, dict) or poc_snapshot.get("round_id") != "single-datatap-smoke":
            raise HTTPException(status.HTTP_409_CONFLICT, "pi_smoke_run_not_allowed")
        event = await self._events.settle_terminal(
            run.id,
            run.user_id,
            RunStatus.COMPLETED,
            {"result_code": "single_datatap_smoke_succeeded"},
            worker_id="pi-poc-smoke",
        )
        if event is None and run.status not in (RunStatus.COMPLETED.value,):
            raise HTTPException(status.HTTP_409_CONFLICT, "pi_smoke_run_not_owned")

    async def settle_tool(
        self, *, token: str, run_id: str, call_id: str, request: PiToolSettled
    ) -> PiToolSettledResponse:
        verify_run_token(token, run_id, settings=self._settings)
        try:
            evidence_id = await PiRunAuditWriter(db=self._db, events=self._events).settle_tool(
                run_id=run_id,
                call_id=call_id,
                raw_payload=request.raw_payload,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_tool_call_not_found") from error
        except ValueError as error:
            if str(error) != "pi_tool_call_not_settleable":
                raise
            raise HTTPException(status.HTTP_409_CONFLICT, "pi_tool_call_not_settleable") from error
        return PiToolSettledResponse(evidence_id=evidence_id)

    async def fail_tool(
        self, *, token: str, run_id: str, call_id: str, request: PiToolFailed
    ) -> None:
        verify_run_token(token, run_id, settings=self._settings)
        safe_error_message = (
            json.dumps(request.error, ensure_ascii=False, default=str)
            if request.error is not None
            else None
        )
        try:
            await PiRunAuditWriter(db=self._db, events=self._events).fail_tool(
                run_id=run_id,
                call_id=call_id,
                status=request.status,
                safe_error_message=safe_error_message,
            )
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_tool_call_not_found") from error
