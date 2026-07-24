"""GoalPolicy 下沉后的上下文组装与 prompt 分派（build_agent_context / agent_decide）。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.session import SessionFactory
from app.identity.models import User
from app.model.contracts import StructuredResult
from app.model.prompts import (
    AGENT_LOOP_PROMPT,
    BRAND_ANALYSIS_LOOP_PROMPT,
    CAMPAIGN_ANALYSIS_LOOP_PROMPT,
)
from app.orchestration.loop import AgentDecision, AgentLoopContext
from app.tasks.dependencies import TaskExecutionDependencies
from app.workspace.models import Message, WorkspaceSession


class _FakeModel:
    def __init__(self) -> None:
        self.requests: list = []

    async def complete_json(self, request):
        self.requests.append(request)
        return StructuredResult(
            value=AgentDecision(action="finish", conclusion="完成"),
            usage=None,
            request_id="req-test",
            regeneration_count=0,
        )


async def _seed_workspace(
    *,
    brand: str = "海底捞",
    category: str | None = "美食",
    brainstorm_profile: dict | None = None,
) -> dict[str, str]:
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = {"user_id": str(uuid4()), "session_id": str(uuid4()), "message_id": str(uuid4())}
    async with SessionFactory.begin() as db:
        db.add(
            User(
                id=ids["user_id"],
                nickname="policy 测试",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            WorkspaceSession(
                id=ids["session_id"],
                user_id=ids["user_id"],
                title="policy 测试会话",
                brand=brand,
                status="active",
                platforms=["xiaohongshu"],
                category=category,
                target_audience="",
                filters_snapshot=(
                    {"brainstorm_profile": brainstorm_profile} if brainstorm_profile else {}
                ),
                is_starred=False,
                last_accessed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        await db.flush()
        db.add(
            Message(
                id=ids["message_id"],
                session_id=ids["session_id"],
                user_id=ids["user_id"],
                role="user",
                content="帮我分析",
                sequence=1,
                metadata_json={},
                created_at=now,
            )
        )
    return ids


async def _cleanup(ids: dict[str, str]) -> None:
    async with SessionFactory.begin() as db:
        await db.execute(delete(Message).where(Message.session_id == ids["session_id"]))
        await db.execute(
            delete(WorkspaceSession).where(WorkspaceSession.id == ids["session_id"])
        )
        await db.execute(delete(User).where(User.id == ids["user_id"]))


def _dependencies(model=None) -> TaskExecutionDependencies:
    deps = TaskExecutionDependencies.__new__(TaskExecutionDependencies)
    deps._transport = None
    deps._model = model
    return deps


@pytest.mark.asyncio
async def test_build_agent_context_injects_contract_only_for_kol() -> None:
    ids = await _seed_workspace()
    try:
        deps = _dependencies()
        kol = await deps.build_agent_context(
            ids["user_id"], ids["session_id"], goal_type="kol_selection"
        )
        assert kol.goal_type == "kol_selection"
        assert kol.export_contract, "kol 必须注入 export_contract"

        brand = await deps.build_agent_context(
            ids["user_id"], ids["session_id"], goal_type="brand_analysis"
        )
        assert brand.goal_type == "brand_analysis"
        assert brand.export_contract == {}

        campaign = await deps.build_agent_context(
            ids["user_id"], ids["session_id"], goal_type="campaign_analysis"
        )
        assert campaign.export_contract == {}
    finally:
        await _cleanup(ids)


@pytest.mark.asyncio
async def test_build_agent_context_merges_goal_params_over_profile() -> None:
    ids = await _seed_workspace(brainstorm_profile={"brand": "旧品牌", "category": "美食"})
    try:
        deps = _dependencies()
        context = await deps.build_agent_context(
            ids["user_id"],
            ids["session_id"],
            goal_type="brand_analysis",
            goal_params={
                "brand": "喜茶",
                "period": {"start": "2026-06-01", "end": "2026-06-30"},
                "platforms": ["douyin"],
            },
        )

        # goal_params 优先：brand 覆盖 brainstorm 画像，period 覆写 requested_period。
        assert context.param_profile["brand"] == "喜茶"
        assert context.param_profile["category"] == "美食"
        assert context.param_profile["platforms"] == ["douyin"]
        assert context.goal_params["brand"] == "喜茶"
        assert context.requested_period["start"] == "2026-06-01"
        assert context.requested_period["end"] == "2026-06-30"
    finally:
        await _cleanup(ids)


@pytest.mark.asyncio
async def test_agent_decide_selects_system_prompt_by_goal_type() -> None:
    model = _FakeModel()
    deps = _dependencies(model)
    for goal_type, prompt, name in (
        ("kol_selection", AGENT_LOOP_PROMPT, "agent_loop_v1"),
        ("brand_analysis", BRAND_ANALYSIS_LOOP_PROMPT, "brand_loop_v1"),
        ("campaign_analysis", CAMPAIGN_ANALYSIS_LOOP_PROMPT, "campaign_loop_v1"),
    ):
        context = AgentLoopContext(
            recent_messages=(),
            tools=(),
            allowed_channels=(),
            goal_type=goal_type,
        )
        await deps.agent_decide(context)

    systems = [request.messages[0].content for request in model.requests]
    assert systems == [
        AGENT_LOOP_PROMPT.system,
        BRAND_ANALYSIS_LOOP_PROMPT.system,
        CAMPAIGN_ANALYSIS_LOOP_PROMPT.system,
    ]
    names = [request.template_name for request in model.requests]
    assert names == ["agent_loop_v1", "brand_loop_v1", "campaign_loop_v1"]
    assert all(request.purpose == "agent_loop" for request in model.requests)
