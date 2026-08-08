"""Pi POC 内部工具桥：只暴露受控历史、Builder 与确定性发布工具。

Pi 可见目录固定为白名单（见 :data:`PI_POC_ALLOWED_TOOLS`）：历史读取
（search_evidence / read_tool_result / read_artifact）、六类强类型 Builder、
get_session_context 与 publish_artifacts。禁止 bash/shell/文件编辑/任意 HTTP/
Draft 直写（create_draft/update_draft/abandon_draft）/计算/记忆等越权工具；
DataTap 由 Task 4 Extension 直连，本 Registry 不注册 ``AgentMcpTool``。
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifactVersion
from app.agent_artifacts.publishing import ArtifactPublicationService
from app.agent_runtime.models import AgentMessage, AgentRun, AgentSession, EvidenceItem, MemoryEntry
from app.agent_runtime.profiles import (
    ARTIFACT_TOOLS,
    HISTORY_TOOLS,
    AgentProfile,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.reviewer import release_run_drafts
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.builders import (
    BuildBrandReportDraftTool,
    BuildCampaignReportDraftTool,
    BuildInsightDraftTool,
    BuildKolAnalysisDraftTool,
    BuildKolDetailDraftTool,
    BuildKolSelectionDraftTool,
)
from app.agent_runtime.tools.contracts import ToolContext, ToolResult
from app.agent_runtime.tools.history import (
    ReadArtifactTool,
    ReadToolResultTool,
    SearchEvidenceTool,
)
from app.agent_runtime.tools.registry import ToolRegistry
from app.marketing_capability_pack.runtime import MarketingRunCapability

# Pi 可见内部工具白名单（设计 §方案 A Task 5；与前端 internal-tools.ts 镜像）。
PI_POC_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "get_session_context",
        "load_marketing_skill",
        "search_evidence",
        "read_tool_result",
        "read_artifact",
        "build_brand_report_draft",
        "build_campaign_report_draft",
        "build_kol_selection_draft",
        "build_kol_analysis_draft",
        "build_kol_detail_draft",
        "build_insight_draft",
        "publish_artifacts",
        "request_clarification",
    }
)

# POC Profile：只放行 history 与 artifact 分类；无 MCP/calculation。
PIPOC_PROFILE = AgentProfile(
    name="pi_poc",
    version="v1",
    allowed_actions=frozenset(),
    allowed_tool_categories=frozenset({HISTORY_TOOLS, ARTIFACT_TOOLS}),
    requires_reviewer=False,
    max_context_budget=0,
    output_schema="agent_actions",
    system_prompt_key="pi_poc_v1",
)


def build_pi_internal_registry(*, db: AsyncSession, worker_id: str) -> ToolRegistry:
    """构建只含白名单工具的 Registry；不注册任何 MCP/计算/Draft 直写工具。"""
    registry = ToolRegistry()
    registry.register(ReadArtifactTool(db), category=HISTORY_TOOLS)
    registry.register(SearchEvidenceTool(db), category=HISTORY_TOOLS)
    registry.register(ReadToolResultTool(db), category=HISTORY_TOOLS)
    registry.register(GetSessionContextTool(db), category=HISTORY_TOOLS)
    registry.register(LoadMarketingSkillTool(db), category=HISTORY_TOOLS)
    registry.register(BuildBrandReportDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildCampaignReportDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildKolSelectionDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildKolAnalysisDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildKolDetailDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildInsightDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(PublishArtifactsTool(db, worker_id=worker_id), category=ARTIFACT_TOOLS)
    registry.register(RequestClarificationTool(db, worker_id=worker_id), category=HISTORY_TOOLS)
    return registry


class GetSessionContextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetSessionContextTool:
    """返回当前 Run 的受限会话上下文（不含任何密钥/完整 Evidence）。"""

    name = "get_session_context"
    input_model = GetSessionContextArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        del arguments
        session = await self._db.get(AgentSession, context.session_id)
        run = await self._db.get(AgentRun, context.run_id)
        if session is None or run is None or session.user_id != context.user_id:
            return ToolResult(
                status="failed", safe_summary="session_not_found", error_type="not_found"
            )
        versions = await self._db.scalars(
            select(
                AgentArtifactVersion.id,
                AgentArtifactVersion.artifact_id,
                AgentArtifactVersion.version,
                AgentArtifactVersion.schema_version,
            )
            .where(AgentArtifactVersion.source_run_id == context.run_id)
            .order_by(AgentArtifactVersion.version)
        )
        evidence_count = await self._db.scalar(
            select(func.count())
            .select_from(EvidenceItem)
            .where(
                EvidenceItem.session_id == context.session_id,
                EvidenceItem.availability_status == "available",
            )
        )
        summary = {
            "run_id": run.id,
            "session_id": session.id,
            "user_id": session.user_id,
            "session_title": session.title,
            "run_status": run.status,
            "profile": run.profile_name,
            "artifact_versions": [
                {"id": row.id, "artifact_id": row.artifact_id, "version": row.version,
                 "schema_version": row.schema_version}
                for row in versions
            ],
            "evidence_count": evidence_count or 0,
        }
        return ToolResult(status="success", safe_summary=json.dumps(summary, ensure_ascii=False))


class LoadMarketingSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    requested_version: str | None = Field(default=None, max_length=64)


class LoadMarketingSkillTool:
    """仅从当前 Run 的已持久化 snapshot 返回专项 Skill，不接收路径。"""

    name = "load_marketing_skill"
    input_model = LoadMarketingSkillArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = LoadMarketingSkillArgs.model_validate(arguments)
        run = await self._db.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == context.run_id,
                AgentRun.user_id == context.user_id,
                AgentRun.session_id == context.session_id,
            )
            .with_for_update()
        )
        if run is None:
            return ToolResult(status="failed", safe_summary="run_not_found", error_type="not_found")
        try:
            capability = MarketingRunCapability.model_validate(
                (run.prompt_snapshot_json or {}).get("marketing_capability_pack")
            )
            loaded = capability.load_skill(args.skill_name, args.requested_version)
        except ValidationError:
            return ToolResult(
                status="failed",
                safe_summary="marketing_skill_snapshot_invalid",
                error_type="marketing_skill_snapshot_invalid",
            )
        except ValueError:
            return ToolResult(
                status="failed",
                safe_summary="marketing_skill_not_enabled",
                error_type="marketing_skill_not_enabled",
            )
        snapshot = dict(run.prompt_snapshot_json or {})
        pack = dict(snapshot.get("marketing_capability_pack") or {})
        loaded_skills = list(pack.get("loaded_skills") or [])
        record = {key: loaded[key] for key in ("name", "version", "digest")}
        if record not in loaded_skills:
            loaded_skills.append(record)
        pack["loaded_skills"] = loaded_skills
        snapshot["marketing_capability_pack"] = pack
        run.prompt_snapshot_json = snapshot
        await self._db.flush()
        return ToolResult(status="success", safe_summary=json.dumps(loaded, ensure_ascii=False))


class PublishArtifactsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_ids: list[str] = Field(min_length=1, max_length=20)


class PublishArtifactsTool:
    """确定性发布：只接受当前 Run 持有的 draft ids（复用既有发布服务，幂等）。"""

    name = "publish_artifacts"
    input_model = PublishArtifactsArgs
    points_cost = 0
    external_side_effect = True

    def __init__(self, db_session: AsyncSession, *, worker_id: str) -> None:
        self._db = db_session
        self._worker_id = worker_id

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = PublishArtifactsArgs.model_validate(arguments)
        service = ArtifactPublicationService(self._db)
        results = await service.publish(
            run_id=context.run_id,
            draft_ids=tuple(dict.fromkeys(args.draft_ids)),
            worker_id=self._worker_id,
        )
        payload = [
            {
                "draft_id": item.draft_id,
                "status": item.status,
                "artifact_id": item.artifact_id,
                "artifact_version_id": item.artifact_version_id,
                "version": item.version,
                "errors": list(item.errors),
            }
            for item in results
        ]
        return ToolResult(status="success", safe_summary=json.dumps(payload, ensure_ascii=False))


class RequestClarificationArgs(BaseModel):
    """Pi 唯一可用的范围澄清出口。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    options: list[str] | None = None

    @field_validator("options")
    @classmethod
    def _options_length(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not 2 <= len(value) <= 4:
            raise ValueError("options must contain 2-4 items when present")
        return value


class RequestClarificationTool:
    """仅写既有澄清消息/Memory 并迁移状态；不调用外部服务或创建 Artifact。"""

    name = "request_clarification"
    input_model = RequestClarificationArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession, *, worker_id: str) -> None:
        self._db = db_session
        self._worker_id = worker_id

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = RequestClarificationArgs.model_validate(arguments)
        run = await self._db.get(AgentRun, context.run_id)
        if run is None or run.user_id != context.user_id or run.session_id != context.session_id:
            return ToolResult(status="failed", safe_summary="run_not_found", error_type="not_found")
        repository = AgentRunRepository(self._db)
        if not await repository.holds_lease(run.id, self._worker_id):
            return ToolResult(
                status="failed", safe_summary="pi_run_lease_not_held", error_type="lease_not_held"
            )
        sequence = await self._db.scalar(
            select(func.max(AgentMessage.sequence)).where(AgentMessage.session_id == run.session_id)
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        self._db.add(
            AgentMessage(
                id=str(uuid4()),
                session_id=run.session_id,
                run_id=run.id,
                role="assistant",
                content=args.question,
                metadata_json={
                    "type": "clarification",
                    "question": args.question,
                    "options": args.options,
                },
                sequence=(sequence or 0) + 1,
                created_at=now,
            )
        )
        self._db.add(
            MemoryEntry(
                id=str(uuid4()),
                session_id=run.session_id,
                source_run_id=run.id,
                memory_type="pending_question",
                content_json={"question": args.question, "options": args.options},
                created_at=now,
            )
        )
        await release_run_drafts(self._db, run.id)
        await repository.transition(run.id, RunStatus.CLARIFICATION_REQUESTED, worker_id=self._worker_id)
        return ToolResult(
            status="success",
            safe_summary=json.dumps({"clarification_requested": True}, ensure_ascii=False),
        )


__all__ = [
    "PIPOC_PROFILE",
    "PI_POC_ALLOWED_TOOLS",
    "GetSessionContextTool",
    "PublishArtifactsTool",
    "RequestClarificationTool",
    "build_pi_internal_registry",
]
