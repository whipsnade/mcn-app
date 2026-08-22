"""Production bridge for the reviewed B0 internal tool registry."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifact
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import AgentEvent, AgentRun
from app.agent_runtime.profiles import ARTIFACT_TOOLS, HISTORY_TOOLS, get_profile
from app.agent_runtime.tools.builders import (
    BuildArtifactDraftTool,
)
from app.agent_runtime.tools.contracts import SERVER_RESERVED_KEYS, ToolResult
from app.agent_runtime.tools.history import ReadArtifactTool
from app.agent_runtime.tools.pi_internal_tools import (
    GetSessionContextTool,
    LoadMarketingSkillTool,
    PublishArtifactsTool,
    RequestClarificationTool,
)
from app.agent_runtime.tools.registry import ToolRegistry


def build_production_internal_registry(*, db: AsyncSession, worker_id: str) -> ToolRegistry:
    """构建生产 Gateway 的 B0 Registry，不依赖 POC 模块或旁路。"""
    # The authenticated route locks the leased Run before dispatching the
    # internal tool. Guard writes therefore share this transaction; opening a
    # second SessionFactory transaction here would wait on the Run lock held by
    # this very request and starve heartbeat/terminal traffic.
    durable_session_factory = None
    registry = ToolRegistry()
    registry.register(ReadArtifactTool(db), category=HISTORY_TOOLS)
    # Pi's direct-result path intentionally has no Evidence search/read bridge.
    # The legacy history tools remain registered only in the current runtime.
    registry.register(GetSessionContextTool(db), category=HISTORY_TOOLS)
    registry.register(LoadMarketingSkillTool(db), category=HISTORY_TOOLS)
    registry.register(
        BuildArtifactDraftTool(db, durable_session_factory=durable_session_factory),
        category=ARTIFACT_TOOLS,
    )
    registry.register(PublishArtifactsTool(db, worker_id=worker_id), category=ARTIFACT_TOOLS)
    registry.register(RequestClarificationTool(db, worker_id=worker_id), category=HISTORY_TOOLS)
    return registry


class ProductionInternalToolBridge:
    """Execute B0 internal tools with identity injected by the lease context."""

    def __init__(
        self,
        *,
        db: AsyncSession | None = None,
        worker_id: str = "pi-gateway",
        registry_factory: Callable[[], ToolRegistry | None] | None = None,
    ) -> None:
        self._db = db
        self._worker_id = worker_id
        self._registry_factory = registry_factory

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        user_id: str,
        session_id: str,
        run_id: str,
        profile_name: str,
        step_id: str | None = None,
    ) -> ToolResult:
        if not all(isinstance(value, str) and value for value in (user_id, session_id, run_id)):
            raise ValueError("pi_gateway_tool_context_required")
        if _contains_reserved_key(arguments):
            raise ValueError("pi_gateway_tool_context_required")
        profile = get_profile(profile_name)
        if self._registry_factory is not None:
            registry = self._registry_factory()
        else:
            if self._db is None:
                raise ValueError("pi_gateway_tool_registry_unavailable")
            registry = build_production_internal_registry(db=self._db, worker_id=self._worker_id)
        if registry is None:
            raise ValueError("pi_gateway_tool_registry_unavailable")
        return await registry.execute(
            internal_name=tool_name,
            arguments=dict(arguments),
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            profile=profile,
            step_id=step_id,
        )


def _contains_reserved_key(value: object) -> bool:
    reserved = {key.lower() for key in SERVER_RESERVED_KEYS | {"tenant_id"}}
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in reserved or _contains_reserved_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_reserved_key(item) for item in value)
    return False


async def append_artifact_tool_events(
    db: AsyncSession, run: AgentRun, tool_name: str, result: ToolResult
) -> list[AgentEvent]:
    """Pi 路径 Artifact 生命周期 SSE 事件（payload 对齐 agent engine 形状）。

    agent 路径由 engine 在工具执行外围发射同族事件；Pi 路径经 internal-tools
    端点执行同一批工具，必须在 Pi 专属层补发——绝不能放进 ToolRegistry/工具
    内部，否则 agent 路径会双发。只在工具成功时发：build_artifact_draft 成功
    → ``artifact.draft.created``（Draft revision > 1 记为 updated）；
    publish_artifacts 成功 → 每个成功发布的 Artifact 一条 ``artifact.published``
    （含 version）。事件经 ``AgentEventStream.append_locked`` 在本请求事务内
    写入（Run 行已被路由的 leased_run 锁定），flush 不 commit，由路由尾统一
    提交并广播。
    """
    if result.status != "success" or tool_name not in ("build_artifact_draft", "publish_artifacts"):
        return []
    try:
        summary = json.loads(result.safe_summary)
    except (TypeError, ValueError):
        return []
    stream = AgentEventStream(db, AgentEventBroker())
    events: list[AgentEvent] = []

    async def _draft_event_payload(artifact: AgentArtifact, draft_id: str, version: int) -> dict[str, Any]:
        return {
            "artifact_id": artifact.id,
            "draft_id": draft_id,
            "module": artifact.module,
            "parent_artifact_id": artifact.parent_artifact_id,
            "status": artifact.status,
            "version": version,
        }

    if tool_name == "build_artifact_draft" and isinstance(summary, dict):
        artifact_id = summary.get("artifact_id")
        draft_id = summary.get("draft_id")
        revision = summary.get("revision")
        if (
            isinstance(artifact_id, str) and artifact_id
            and isinstance(draft_id, str) and draft_id
        ):
            artifact = await db.get(AgentArtifact, artifact_id)
            if artifact is not None:
                version = revision if isinstance(revision, int) else 0
                event_type = "artifact.draft.updated" if version > 1 else "artifact.draft.created"
                events.append(
                    await stream.append_locked(
                        run, event_type, await _draft_event_payload(artifact, draft_id, version)
                    )
                )
        return events

    if tool_name == "publish_artifacts" and isinstance(summary, list):
        for item in summary:
            if not isinstance(item, dict) or item.get("status") != "published":
                continue
            artifact_id = item.get("artifact_id")
            version = item.get("version")
            if not isinstance(artifact_id, str) or not artifact_id:
                continue
            artifact = await db.get(AgentArtifact, artifact_id)
            if artifact is None:  # pragma: no cover - FK 保证稳定身份存在
                continue
            events.append(
                await stream.append_locked(
                    run,
                    "artifact.published",
                    {
                        "artifact_id": artifact.id,
                        "module": artifact.module,
                        "parent_artifact_id": artifact.parent_artifact_id,
                        "status": artifact.status,
                        "version": version if isinstance(version, int) else 0,
                    },
                )
            )
    return events
