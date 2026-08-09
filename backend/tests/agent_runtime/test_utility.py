"""UtilityRunner 集成测试（Task 15 / 设计文档 §五 utility_v1 / §8.1）。

覆盖：
1. 三个 Utility 任务（会话标题 / Run 摘要 / 建议）都创建 run_kind=internal、
   visibility=internal、profile=utility_v1 的内部 Run；
2. 上下文是受限短上下文（recent_messages 有界，不注入完整历史）；
3. 强类型结果正确落库：标题 → agent_sessions.title、摘要 → memory_entry、
   建议 → 完成消息 metadata；
4. Utility 失败是 best-effort：只记内部 Run 失败，绝不改变父 Run 结果；
5. 上下文序列化必须落在 max_context_chars 预算内（含超大 Artifact 目录）。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.agent_artifacts.models import AgentArtifact
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    MemoryEntry,
)
from app.agent_runtime.repository import utc_now
from app.agent_runtime.utility import UTILITY_PROFILE_NAME, UtilityRunner
from app.model.contracts import ChatMessage
from app.tenancy.models import Tenant, TenantMembership
from app.tenancy.service import TenantService


class FakeUtilityGateway:
    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def decide(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("fake utility gateway exhausted")
        return self.outcomes.pop(0)


class BoomGateway:
    async def decide(self, **kwargs: Any) -> Any:
        raise RuntimeError("utility model call failed")


async def _make_session_with_messages(db_session, user_factory, *, message_count: int = 4):
    user = await user_factory()
    now = utc_now()
    tenant_id = (await TenantService(db_session).resolve_user(user.id)).tenant_id
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        tenant_id=tenant_id,
        title="新会话1",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    for seq in range(1, message_count + 1):
        db_session.add(
            AgentMessage(
                id=str(uuid4()),
                session_id=session.id,
                run_id=None,
                role="user",
                content=f"第 {seq} 条消息",
                metadata_json=None,
                sequence=seq,
                created_at=now,
            )
        )
    await db_session.flush()
    return session, user


async def _parent_run(
    db_session, session: AgentSession, user, *, status: str = "completed", with_assistant: bool = True
):
    now = utc_now()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status=status,
        outcome="completed" if status == "completed" else None,
        decision_count=1,
        review_count=0,
        revision_count=0,
        completed_at=now if status == "completed" else None,
    )
    db_session.add(run)
    await db_session.flush()
    if with_assistant:
        assistant = AgentMessage(
            id=str(uuid4()),
            session_id=session.id,
            run_id=run.id,
            role="assistant",
            content="分析完成",
            metadata_json={"type": "completion"},
            sequence=1000,
            created_at=now,
        )
        db_session.add(assistant)
        await db_session.flush()
    return run


def _make_runner(db_session, gateway: Any) -> UtilityRunner:
    return UtilityRunner(db=db_session, gateway=gateway, worker_id="utility-worker")


async def _internal_run(db_session, *, parent_run_id: str | None):
    stmt = select(AgentRun).where(AgentRun.run_kind == "internal")
    if parent_run_id is not None:
        stmt = stmt.where(AgentRun.parent_run_id == parent_run_id)
    else:
        stmt = stmt.where(AgentRun.parent_run_id.is_(None))
    return await db_session.scalar(stmt)


async def _context_from_call(call: dict[str, Any]) -> dict[str, Any]:
    messages: list[ChatMessage] = call["messages"]
    return json.loads(messages[0].content)


# ---------------------------------------------------------------------------
# 1. run_summary：内部 Run + memory_entry
# ---------------------------------------------------------------------------


async def test_run_summary_creates_internal_run_and_writes_memory(db_session, user_factory) -> None:
    session, user = await _make_session_with_messages(db_session, user_factory)
    run = await _parent_run(db_session, session, user)
    gateway = FakeUtilityGateway([{"task": "run_summary", "summary": "用户分析了瑞幸咖啡的声量与情感"}])
    runner = _make_runner(db_session, gateway)

    result = await runner.generate_run_summary(run=run)

    assert result == "用户分析了瑞幸咖啡的声量与情感"
    internal = await _internal_run(db_session, parent_run_id=run.id)
    assert internal is not None
    assert internal.run_kind == "internal"
    assert internal.visibility == "internal"
    assert internal.profile_name == UTILITY_PROFILE_NAME
    assert internal.status == "completed"
    assert internal.parent_run_id == run.id
    # 摘要写入 memory_entry（run_summary）
    entry = await db_session.scalar(
        select(MemoryEntry).where(
            MemoryEntry.memory_type == "run_summary", MemoryEntry.source_run_id == run.id
        )
    )
    assert entry is not None
    assert entry.content_json["summary"] == "用户分析了瑞幸咖啡的声量与情感"
    assert entry.session_id == session.id
    # 上下文携带 task 标记
    context = await _context_from_call(gateway.calls[0])
    assert context["task"] == "run_summary"
    assert context["parent_run"]["run_id"] == run.id


async def test_run_summary_context_is_bounded(db_session, user_factory) -> None:
    session, user = await _make_session_with_messages(db_session, user_factory, message_count=15)
    run = await _parent_run(db_session, session, user, with_assistant=False)
    gateway = FakeUtilityGateway([{"task": "run_summary", "summary": "摘要"}])
    runner = UtilityRunner(db=db_session, gateway=gateway, worker_id="utility-worker", recent_message_window=6)

    await runner.generate_run_summary(run=run)

    context = await _context_from_call(gateway.calls[0])
    # 只读有界短上下文：15 条历史只注入最近 6 条，绝不注入完整历史
    assert len(context["recent_messages"]) == 6
    assert [m["sequence"] for m in context["recent_messages"]] == [10, 11, 12, 13, 14, 15]


# ---------------------------------------------------------------------------
# 2. session_title：内部 Run + agent_sessions.title
# ---------------------------------------------------------------------------


async def test_session_title_updates_session(db_session, user_factory) -> None:
    session, user = await _make_session_with_messages(db_session, user_factory)
    gateway = FakeUtilityGateway([{"task": "session_title", "title": "瑞幸品牌声量分析"}])
    runner = _make_runner(db_session, gateway)

    result = await runner.generate_session_title(session_id=session.id, user_id=user.id)

    assert result == "瑞幸品牌声量分析"
    fresh = await db_session.get(AgentSession, session.id)
    assert fresh.title == "瑞幸品牌声量分析"
    internal = await _internal_run(db_session, parent_run_id=None)
    assert internal is not None
    assert internal.run_kind == "internal"
    assert internal.visibility == "internal"
    assert internal.profile_name == UTILITY_PROFILE_NAME


async def test_session_title_not_overwritten_after_user_rename(db_session, user_factory) -> None:
    """重命名保护（§6.4）：标题已不是系统默认「新会话N」时不得覆盖，
    且不浪费模型调用（提前返回，不建内部 Run）。"""
    session, user = await _make_session_with_messages(db_session, user_factory)
    session.title = "用户自己改的名字"
    await db_session.flush()
    gateway = FakeUtilityGateway([{"task": "session_title", "title": "模型起的标题"}])
    runner = _make_runner(db_session, gateway)

    result = await runner.generate_session_title(session_id=session.id, user_id=user.id)

    assert result is None
    fresh = await db_session.get(AgentSession, session.id)
    assert fresh.title == "用户自己改的名字"
    assert gateway.calls == []
    assert await _internal_run(db_session, parent_run_id=None) is None


async def test_session_title_does_not_create_internal_run_when_utility_license_is_suspended(
    db_session, user_factory
) -> None:
    session, user = await _make_session_with_messages(db_session, user_factory)
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    tenant = await db_session.get(Tenant, membership.tenant_id)
    assert tenant is not None
    tenant.license_status = "suspended"
    await db_session.flush()

    gateway = FakeUtilityGateway([{"task": "session_title", "title": "不应调用"}])
    result = await _make_runner(db_session, gateway).generate_session_title(
        session_id=session.id, user_id=user.id
    )

    assert result is None
    assert gateway.calls == []
    assert await _internal_run(db_session, parent_run_id=None) is None


# ---------------------------------------------------------------------------
# 3. suggestions：内部 Run + 完成消息 metadata
# ---------------------------------------------------------------------------


async def test_suggestions_written_to_completion_message(db_session, user_factory) -> None:
    session, user = await _make_session_with_messages(db_session, user_factory)
    run = await _parent_run(db_session, session, user)
    gateway = FakeUtilityGateway([{"task": "suggestions", "suggestions": ["继续看竞品", "查看达人分布"]}])
    runner = _make_runner(db_session, gateway)

    result = await runner.generate_suggestions(run=run)

    assert result == ["继续看竞品", "查看达人分布"]
    assistant = await db_session.scalar(
        select(AgentMessage).where(
            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
        )
    )
    assert assistant is not None
    assert assistant.metadata_json is not None
    assert assistant.metadata_json["suggestions"] == ["继续看竞品", "查看达人分布"]
    internal = await _internal_run(db_session, parent_run_id=run.id)
    assert internal is not None
    assert internal.profile_name == UTILITY_PROFILE_NAME


# ---------------------------------------------------------------------------
# 4. 失败隔离：Utility 失败不改变父 Run 结果
# ---------------------------------------------------------------------------


async def test_utility_failure_does_not_change_parent_run_outcome(db_session, user_factory) -> None:
    session, user = await _make_session_with_messages(db_session, user_factory)
    run = await _parent_run(db_session, session, user, status="completed")
    runner = _make_runner(db_session, BoomGateway())

    result = await runner.generate_run_summary(run=run)

    assert result is None
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == "completed"
    assert fresh.outcome == "completed"
    assert fresh.completed_at is not None
    # 内部 Utility Run 以 failed 收口，不污染父 Run
    internal = await _internal_run(db_session, parent_run_id=run.id)
    assert internal is not None
    assert internal.status == "failed"
    assert internal.error_code == "utility_failed"
    # 无残留摘要
    assert (
        await db_session.scalar(
            select(MemoryEntry).where(MemoryEntry.source_run_id == run.id)
        )
    ) is None


# ---------------------------------------------------------------------------
# 5. 上下文硬预算：超大 Artifact 目录仍不超 max_context_chars（Fix 3）
# ---------------------------------------------------------------------------


async def test_context_respects_hard_char_budget_with_huge_artifact_directory(
    db_session, user_factory
) -> None:
    session, user = await _make_session_with_messages(db_session, user_factory, message_count=30)
    now = utc_now()
    # 大量 Artifact 目录条目（紧凑投影仍会膨胀序列化体积）
    for index in range(60):
        artifact = AgentArtifact(
            id=str(uuid4()),
            session_id=session.id,
            user_id=user.id,
            module="brand",
            artifact_type="brand_report_v3",
            artifact_key=f"brand:{index}:{('x' * 40)}",
            status="published",
            latest_version=index + 1,
            activity_sequence=index,
            created_at=now,
            updated_at=now,
        )
        db_session.add(artifact)
    await db_session.flush()
    run = await _parent_run(db_session, session, user, with_assistant=False)
    gateway = FakeUtilityGateway([{"task": "run_summary", "summary": "摘要"}])
    runner = UtilityRunner(
        db=db_session,
        gateway=gateway,
        worker_id="utility-worker",
        recent_message_window=6,
        max_context_chars=500,
        artifact_directory_cap=10,
    )

    await runner.generate_run_summary(run=run)

    context = await _context_from_call(gateway.calls[0])
    serialized = json.dumps(context, ensure_ascii=False, default=str)
    assert len(serialized) <= 500  # 硬预算：即使目录很大也不超
    assert context.get("truncated") is True
    # 目录被压到上限内
    assert len(context.get("artifact_directory", [])) <= 10
