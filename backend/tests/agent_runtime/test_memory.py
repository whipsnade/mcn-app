"""分层记忆上下文构建器测试（设计文档 §九）。

默认上下文只含：当前用户消息、有限条最近消息、Session Summary、历史 Run
摘要、紧凑 Artifact 目录（类型/版本/范围/父子关系/数据状态）、可用工具与
成本、钱包余额。不自动注入完整 Evidence 或完整历史报告 payload。跨用户读取
他人 Session 的上下文被拒绝。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
)
from app.agent_runtime.memory import (
    MemoryContextBuilder,
    MemorySessionForbidden,
    MemorySessionNotFound,
)
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    MemoryEntry,
)
from app.agent_runtime.profiles import (
    CALCULATION_TOOLS,
    HISTORY_TOOLS,
    PROFILES,
)
from app.agent_runtime.tools.calculation import CalculateExpressionTool
from app.agent_runtime.tools.history import ReadArtifactTool
from app.agent_runtime.tools.registry import ToolRegistry
from app.billing.service import WalletService

session_analyst = PROFILES["session_analyst_v1"]

BIG_PAYLOAD_MARKER = "非常长的一个产品报告正文" * 2000


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_session(db_session, user_id: str) -> AgentSession:
    """创建含消息、Run 摘要、带大 payload 的已发布 Artifact 的会话。"""
    now = _now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user_id,
        title="品牌分析会话",
        status="active",
        session_summary="用户关注美妆品牌声量，已产出品牌声量分析。",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()

    # 最近消息窗口：12 条已持久化消息。
    for index in range(12):
        db_session.add(
            AgentMessage(
                id=str(uuid4()),
                session_id=session.id,
                run_id=None,
                role="user" if index % 2 == 0 else "assistant",
                content=f"历史消息-{index}",
                sequence=index,
                created_at=now,
            )
        )
    await db_session.flush()

    # 一个 Run + 历史 Run 摘要。
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="completed",
        outcome="completed",
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        MemoryEntry(
            id=str(uuid4()),
            session_id=session.id,
            source_run_id=run.id,
            memory_type="run_summary",
            content_json={"summary": "第一轮完成品牌声量趋势分析"},
            created_at=now,
        )
    )
    await db_session.flush()

    # Artifact + 已发布版本（payload 很大，绝不能进入默认上下文）。
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        module="brand",
        artifact_type="brand_report_v2",
        parent_artifact_id=None,
        artifact_key="brand/声量分析",
        status="published",
        latest_version=1,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session.id,
        owner_run_id=run.id,
        current_revision=0,
        status="idle",
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()
    draft_rev = ArtifactDraftRevision(
        id=str(uuid4()),
        draft_id=draft.id,
        artifact_id=artifact.id,
        run_id=run.id,
        revision=0,
        schema_version="v2",
        payload_json={"title": BIG_PAYLOAD_MARKER, "data": {"rows": [{"k": f"v{i}"} for i in range(50)]}},
        payload_hash="h" * 64,
        created_at=now,
    )
    db_session.add(draft_rev)
    await db_session.flush()
    db_session.add(
        AgentArtifactVersion(
            id=str(uuid4()),
            artifact_id=artifact.id,
            version=1,
            source_run_id=run.id,
            source_draft_revision_id=draft_rev.id,
            schema_version="v2",
            payload_json={"title": BIG_PAYLOAD_MARKER, "data": {"rows": [{"k": f"v{i}"} for i in range(50)]}},
            data_status="complete",
            created_at=now,
        )
    )
    await db_session.flush()

    # 一个子 Artifact（有 parent）验证父子关系进入目录。
    child = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        module="insight",
        artifact_type="insight_board_v1",
        parent_artifact_id=artifact.id,
        artifact_key="insight/品牌洞察",
        status="published",
        latest_version=1,
        activity_sequence=1,
        created_at=now,
        updated_at=now,
    )
    db_session.add(child)
    await db_session.flush()
    return session


def _registry(db_session) -> ToolRegistry:
    registry = ToolRegistry()
    # 真实工具注册同时验证它们满足 TrustedTool 契约。
    registry.register(ReadArtifactTool(db_session), category=HISTORY_TOOLS)
    registry.register(CalculateExpressionTool(), category=CALCULATION_TOOLS)
    return registry


# ---------------------------------------------------------------------------
# 默认上下文内容契约
# ---------------------------------------------------------------------------


async def test_default_context_contains_only_bounded_sections(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _seed_session(db_session, user.id)
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    context = await builder.build(
        user_id=user.id,
        session_id=session.id,
        profile=session_analyst,
        current_user_message="继续分析",
    )

    assert set(context.keys()) == {
        "current_user_message",
        "recent_messages",
        "session_summary",
        "run_summaries",
        "artifact_directory",
        "available_tools",
        "wallet",
    }
    assert context["current_user_message"] == "继续分析"
    assert context["session_summary"] == "用户关注美妆品牌声量，已产出品牌声量分析。"
    assert len(context["run_summaries"]) == 1
    assert context["run_summaries"][0]["summary"] == "第一轮完成品牌声量趋势分析"


async def test_recent_messages_window_is_bounded(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _seed_session(db_session, user.id)
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    context = await builder.build(
        user_id=user.id,
        session_id=session.id,
        profile=session_analyst,
        current_user_message="继续分析",
    )
    # 只返回有限最近消息窗口，按时间正序。
    messages = context["recent_messages"]
    assert len(messages) == 8
    assert [m["sequence"] for m in messages] == [4, 5, 6, 7, 8, 9, 10, 11]
    assert [m["content"] for m in messages][0] == "历史消息-4"


async def test_artifact_directory_is_compact_without_payload(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _seed_session(db_session, user.id)
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    context = await builder.build(
        user_id=user.id,
        session_id=session.id,
        profile=session_analyst,
        current_user_message="继续分析",
    )
    directory = context["artifact_directory"]
    by_key = {entry["artifact_key"]: entry for entry in directory}
    parent = by_key["brand/声量分析"]
    # 目录条目只含类型/版本/范围/父子关系/数据状态，绝不含 payload。
    assert set(parent.keys()) == {
        "artifact_id",
        "artifact_key",
        "module",
        "artifact_type",
        "version",
        "parent_artifact_id",
        "status",
        "data_status",
        "updated_at",
    }
    assert parent["artifact_type"] == "brand_report_v2"
    assert parent["version"] == 1
    assert parent["data_status"] == "complete"
    assert parent["parent_artifact_id"] is None
    child = by_key["insight/品牌洞察"]
    assert child["artifact_type"] == "insight_board_v1"
    assert child["parent_artifact_id"] == parent["artifact_id"]
    # 默认上下文整体绝不嵌入完整 payload。
    assert BIG_PAYLOAD_MARKER not in json.dumps(context, ensure_ascii=False)


async def test_default_context_does_not_inject_evidence(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _seed_session(db_session, user.id)
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    context = await builder.build(
        user_id=user.id,
        session_id=session.id,
        profile=session_analyst,
        current_user_message="继续分析",
    )
    serialized = json.dumps(context, ensure_ascii=False)
    # 无任何 evidence / 原始报告数据。
    assert "evidence" not in serialized
    assert "raw_payload" not in serialized
    assert "normalized_preview" not in serialized


async def test_context_includes_tool_catalog_and_wallet(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _seed_session(db_session, user.id)
    await WalletService(db_session).ensure_welcome_grant(user.id)
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    context = await builder.build(
        user_id=user.id,
        session_id=session.id,
        profile=session_analyst,
        current_user_message="继续分析",
    )
    tools = {t["internal_name"]: t for t in context["available_tools"]}
    assert set(tools.keys()) == {"read_artifact", "calculate_expression"}
    # 确定性工具零积分；目录带成本与分类。
    assert tools["read_artifact"]["points_cost"] == 0
    assert tools["read_artifact"]["category"] == HISTORY_TOOLS
    assert tools["calculate_expression"]["points_cost"] == 0
    assert tools["calculate_expression"]["category"] == CALCULATION_TOOLS
    # 钱包余额：welcome grant 1000。
    assert context["wallet"]["balance"] == 1000


async def test_context_wallet_missing_balance_zero(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _seed_session(db_session, user.id)
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    context = await builder.build(
        user_id=user.id,
        session_id=session.id,
        profile=session_analyst,
        current_user_message="继续分析",
    )
    assert context["wallet"] == {"balance": 0, "reserved": 0}


# ---------------------------------------------------------------------------
# 用户隔离
# ---------------------------------------------------------------------------


async def test_context_for_other_users_session_is_rejected(db_session, user_factory) -> None:
    owner = await user_factory()
    other = await user_factory()
    session = await _seed_session(db_session, owner.id)
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    with pytest.raises(MemorySessionForbidden):
        await builder.build(
            user_id=other.id,
            session_id=session.id,
            profile=session_analyst,
            current_user_message="继续分析",
        )


async def test_context_for_missing_session_is_rejected(db_session, user_factory) -> None:
    user = await user_factory()
    builder = MemoryContextBuilder(db_session, _registry(db_session))

    with pytest.raises(MemorySessionNotFound):
        await builder.build(
            user_id=user.id,
            session_id="no-such-session",
            profile=session_analyst,
            current_user_message="继续分析",
        )
