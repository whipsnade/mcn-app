"""Production bridge for the reviewed B0 internal tool registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.profiles import ARTIFACT_TOOLS, HISTORY_TOOLS, get_profile
from app.agent_runtime.tools.builders import (
    BuildBrandReportDraftTool,
    BuildCampaignReportDraftTool,
    BuildInsightDraftTool,
    BuildKolAnalysisDraftTool,
    BuildKolDetailDraftTool,
    BuildKolSelectionDraftTool,
)
from app.agent_runtime.tools.contracts import SERVER_RESERVED_KEYS, ToolResult
from app.agent_runtime.tools.history import ReadArtifactTool, ReadToolResultTool, SearchEvidenceTool
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
    registry.register(
        SearchEvidenceTool(db, durable_session_factory=durable_session_factory),
        category=HISTORY_TOOLS,
    )
    registry.register(ReadToolResultTool(db), category=HISTORY_TOOLS)
    registry.register(GetSessionContextTool(db), category=HISTORY_TOOLS)
    registry.register(LoadMarketingSkillTool(db), category=HISTORY_TOOLS)
    registry.register(
        BuildBrandReportDraftTool(db, durable_session_factory=durable_session_factory),
        category=ARTIFACT_TOOLS,
    )
    registry.register(
        BuildCampaignReportDraftTool(db, durable_session_factory=durable_session_factory),
        category=ARTIFACT_TOOLS,
    )
    registry.register(
        BuildKolSelectionDraftTool(db, durable_session_factory=durable_session_factory),
        category=ARTIFACT_TOOLS,
    )
    registry.register(
        BuildKolAnalysisDraftTool(db, durable_session_factory=durable_session_factory),
        category=ARTIFACT_TOOLS,
    )
    registry.register(
        BuildKolDetailDraftTool(db, durable_session_factory=durable_session_factory),
        category=ARTIFACT_TOOLS,
    )
    registry.register(
        BuildInsightDraftTool(db, durable_session_factory=durable_session_factory),
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
