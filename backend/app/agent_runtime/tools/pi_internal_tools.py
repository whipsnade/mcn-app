"""共用的 Pi 内部工具实现。

这里仅放工具 DTO/执行器；POC 与生产 Gateway 分别组装自己的 Registry，
因此生产桥不会依赖 POC 的设置守卫、审计旁路或路由。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifactVersion
from app.agent_artifacts.model_inputs import (
    MODEL_INPUT_BY_ARTIFACT_TYPE,
    model_input_contract,
)
from app.agent_artifacts.publishing import ArtifactPublicationService
from app.agent_runtime.models import AgentMessage, AgentRun, AgentSession, MemoryEntry
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.reviewer import release_run_drafts
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.contracts import ToolContext, ToolResult
from app.marketing_capability_pack.runtime import MarketingRunCapability

#: load_marketing_skill 返回体 fail-closed 上限（skill 正文 + 模型输入契约
#: 完整 JSON Schema；超出即拒绝，防止撑爆模型上下文）。
_MAX_SKILL_PAYLOAD_BYTES = 512 * 1024


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
            return ToolResult(status="failed", safe_summary="session_not_found", error_type="not_found")
        # 多列查询必须用 execute().all() 取 Row；scalars() 只会返回首列
        # （str），下方 row.id 将抛 AttributeError。
        versions = (
            await self._db.execute(
                select(
                    AgentArtifactVersion.id,
                    AgentArtifactVersion.artifact_id,
                    AgentArtifactVersion.version,
                    AgentArtifactVersion.schema_version,
                )
                .where(AgentArtifactVersion.source_run_id == context.run_id)
                .order_by(AgentArtifactVersion.version)
            )
        ).all()
        summary = {
            "run_id": run.id,
            "session_id": session.id,
            "user_id": session.user_id,
            "session_title": session.title,
            "run_status": run.status,
            "profile": run.profile_name,
            "artifact_versions": [
                {
                    "id": row.id,
                    "artifact_id": row.artifact_id,
                    "version": row.version,
                    "schema_version": row.schema_version,
                }
                for row in versions
            ],
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
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if run is None:
            return ToolResult(status="failed", safe_summary="run_not_found", error_type="not_found")
        try:
            # The capability pack is a Run-time trust input.  Prompt snapshots
            # are model-facing/audit data and must never be able to widen the
            # set of Skills or artifact contracts after Run creation.
            capability = MarketingRunCapability.model_validate(
                (run.runtime_config_snapshot_json or {}).get("capability_pack")
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
        # 已注册模型输入契约的 Artifact 类型：把精确 DTO JSON Schema / 合法示例 /
        # 发布预期一并交给模型（单一事实源来自 model_input_contract，不手写第二份）。
        artifact_contract = loaded.get("artifact_contract")
        if (
            isinstance(artifact_contract, str)
            and artifact_contract in MODEL_INPUT_BY_ARTIFACT_TYPE
        ):
            loaded["model_input_contract"] = model_input_contract(artifact_contract)
        rendered = json.dumps(loaded, ensure_ascii=False)
        if len(rendered.encode("utf-8")) > _MAX_SKILL_PAYLOAD_BYTES:
            return ToolResult(
                status="failed",
                safe_summary="marketing_skill_contract_too_large",
                error_type="marketing_skill_contract_too_large",
            )
        snapshot = dict(run.prompt_snapshot_json or {})
        loaded_skills = list(snapshot.get("loaded_marketing_skills") or [])
        record = {key: loaded[key] for key in ("name", "version", "digest")}
        if record not in loaded_skills:
            loaded_skills.append(record)
        snapshot["loaded_marketing_skills"] = loaded_skills
        run.prompt_snapshot_json = snapshot
        await self._db.flush()
        return ToolResult(status="success", safe_summary=rendered)


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
        results = await ArtifactPublicationService(self._db).publish(
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
            return ToolResult(status="failed", safe_summary="pi_run_lease_not_held", error_type="lease_not_held")
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
    "GetSessionContextTool",
    "LoadMarketingSkillTool",
    "PublishArtifactsTool",
    "RequestClarificationTool",
]
