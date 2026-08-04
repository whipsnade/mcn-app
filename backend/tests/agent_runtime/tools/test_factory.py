"""生产工具装配工厂（设计 §5.1）与渠道权限注入测试。

覆盖：
1. ``AgentToolRegistryFactory`` 注册全部内部工具（history 3 + calculation 5
   + artifact 2），且分类正确、可执行；
2. MCP 目录行按审核状态过滤（approved + enabled 才可见；不在审核 allowlist
   的目录行不挂执行器）；
3. ``load_channel_permissions`` 只返回启用中的渠道；
4. 渠道权限注入后平台型 MCP 工具可见/不可见；
5. ``kol_detail_v1`` Profile 只见明确 allowlist 的 MCP 工具 + artifact 工具；
6. ``main.create_agent_runtime`` 的 engine_factory 装配路径（生产接线回归）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.profiles import (
    ARTIFACT_TOOLS,
    CALCULATION_TOOLS,
    HISTORY_TOOLS,
    MCP_TOOLS,
    PROFILES,
)
from app.agent_runtime.tools.factory import (
    AgentToolRegistryFactory,
    load_channel_permissions,
)
from app.agent_runtime.tools.registry import UnknownToolError
from app.identity.models import UserChannelPermission
from app.mcp_gateway.models import McpToolCatalog

session_analyst = PROFILES["session_analyst_v1"]
kol_detail_profile = PROFILES["kol_detail_v1"]

# 设计 §5.1：生产必须注册的内部工具全集。
_INTERNAL_HISTORY = {"read_artifact", "search_evidence", "read_tool_result"}
_INTERNAL_CALCULATION = {
    "calculate_expression",
    "aggregate_metrics",
    "calculate_period_comparison",
    "normalize_sentiment",
    "rank_kols",
}
_INTERNAL_ARTIFACT = {"create_draft", "update_draft"}


async def _add_catalog_row(
    db_session,
    *,
    internal_name: str,
    service_slug: str = "insight-cube-mcp",
    review_status: str = "approved",
    is_enabled: bool = True,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        McpToolCatalog(
            id=str(uuid4()),
            service_slug=service_slug,
            internal_tool_name=internal_name,
            reviewed_description=f"{internal_name} 描述",
            input_schema_json={"type": "object"},
            output_validator_version="v1",
            discovery_digest="d" * 64,
            review_status=review_status,
            is_enabled=is_enabled,
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()


# ---------------------------------------------------------------------------
# 1. 内部工具装配
# ---------------------------------------------------------------------------


async def test_factory_registers_all_internal_tools(db_session) -> None:
    registry = AgentToolRegistryFactory().build(db_session)
    entries = {entry.internal_name: entry for entry in registry.registered_tools}

    assert _INTERNAL_HISTORY | _INTERNAL_CALCULATION | _INTERNAL_ARTIFACT <= set(entries)
    for name in _INTERNAL_HISTORY:
        assert entries[name].category == HISTORY_TOOLS
        assert entries[name].tool is not None
    for name in _INTERNAL_CALCULATION:
        assert entries[name].category == CALCULATION_TOOLS
        assert entries[name].tool is not None
    for name in _INTERNAL_ARTIFACT:
        assert entries[name].category == ARTIFACT_TOOLS
        assert entries[name].tool is not None


async def test_factory_internal_tools_visible_to_session_analyst(db_session) -> None:
    registry = AgentToolRegistryFactory().build(db_session)
    visible = await registry.visible_tools(session_analyst)
    names = {entry.internal_name for entry in visible}
    assert _INTERNAL_HISTORY | _INTERNAL_CALCULATION | _INTERNAL_ARTIFACT <= names


# ---------------------------------------------------------------------------
# 2. MCP 目录审核过滤
# ---------------------------------------------------------------------------


async def test_factory_filters_catalog_by_review_status(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    await _add_catalog_row(
        db_session,
        internal_name="social_statistic_trend",
        review_status="quarantined",
    )
    await _add_catalog_row(
        db_session,
        internal_name="social_statistic_overview",
        is_enabled=False,
    )

    registry = AgentToolRegistryFactory().build(db_session)
    visible = await registry.visible_tools(session_analyst)
    mcp_names = {entry.internal_name for entry in visible if entry.category == MCP_TOOLS}
    assert "query_analysis_data" in mcp_names
    assert "social_statistic_trend" not in mcp_names
    assert "social_statistic_overview" not in mcp_names


async def test_factory_does_not_attach_executor_outside_allowlist(db_session) -> None:
    # 目录行 approved+enabled 但不在审核 allowlist 内：可见但不可执行。
    await _add_catalog_row(db_session, internal_name="unreviewed_custom_tool")

    registry = AgentToolRegistryFactory().build(db_session)
    # 目录在 visible_tools/_ensure_catalog 时才加载，先触发一次加载。
    await registry.visible_tools(session_analyst)
    entries = {entry.internal_name: entry for entry in registry.registered_tools}
    assert entries["unreviewed_custom_tool"].tool is None

    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="unreviewed_custom_tool",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=session_analyst,
        )


# ---------------------------------------------------------------------------
# 3. 渠道权限数据源
# ---------------------------------------------------------------------------


async def test_load_channel_permissions_returns_enabled_only(db_session, user_factory) -> None:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        UserChannelPermission(
            id=str(uuid4()),
            user_id=user.id,
            channel="xiaohongshu",
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        UserChannelPermission(
            id=str(uuid4()),
            user_id=user.id,
            channel="douyin",
            is_enabled=False,
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()

    granted = await load_channel_permissions(db_session, user.id)
    assert granted == frozenset({"xiaohongshu"})


async def test_load_channel_permissions_empty_by_default(db_session, user_factory) -> None:
    user = await user_factory()
    assert await load_channel_permissions(db_session, user.id) == frozenset()


# ---------------------------------------------------------------------------
# 4. 渠道权限过滤平台型 MCP 工具
# ---------------------------------------------------------------------------


async def test_factory_channel_permissions_gate_platform_tools(db_session) -> None:
    await _add_catalog_row(
        db_session, internal_name="general_search", service_slug="bilibili-mcp"
    )

    registry = AgentToolRegistryFactory().build(db_session)
    denied = await registry.visible_tools(session_analyst, channel_permissions=())
    assert "general_search" not in {entry.internal_name for entry in denied}

    granted = await registry.visible_tools(session_analyst, channel_permissions={"bilibili"})
    assert "general_search" in {entry.internal_name for entry in granted}


# ---------------------------------------------------------------------------
# 5. kol_detail_v1 明确 allowlist
# ---------------------------------------------------------------------------


async def test_kol_detail_profile_sees_only_allowlisted_mcp_tools(db_session) -> None:
    await _add_catalog_row(
        db_session, internal_name="kol_detail", service_slug="social-grow-mcp"
    )
    await _add_catalog_row(db_session, internal_name="query_raw_posts")
    await _add_catalog_row(db_session, internal_name="query_analysis_data")

    registry = AgentToolRegistryFactory().build(db_session)
    visible = await registry.visible_tools(kol_detail_profile)
    names = {entry.internal_name for entry in visible}
    # kol_detail_v1 保留 Artifact Builder/发布能力（修复设计 §5.1）：五个
    # Builder 工具与 create/update_draft 同属 ARTIFACT_TOOLS 分类。
    builder_tools = {
        "build_brand_report_draft",
        "build_campaign_report_draft",
        "build_kol_selection_draft",
        "build_kol_analysis_draft",
        "build_kol_detail_draft",
    }
    assert names == {
        "kol_detail",
        "query_raw_posts",
        "create_draft",
        "update_draft",
        *builder_tools,
    }


async def test_kol_detail_profile_cannot_execute_other_mcp_tools(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="query_analysis_data")

    registry = AgentToolRegistryFactory().build(db_session)
    await registry.visible_tools(kol_detail_profile)  # 触发目录加载
    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="query_analysis_data",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=kol_detail_profile,
        )


async def test_session_analyst_still_sees_all_approved_mcp_tools(db_session) -> None:
    # 非 kol_detail Profile 不受 allowlist 限制。
    await _add_catalog_row(db_session, internal_name="query_analysis_data")

    registry = AgentToolRegistryFactory().build(db_session)
    visible = await registry.visible_tools(session_analyst)
    assert "query_analysis_data" in {entry.internal_name for entry in visible}


# ---------------------------------------------------------------------------
# 6. main.create_agent_runtime 装配路径
# ---------------------------------------------------------------------------


async def test_main_engine_factory_assembles_full_registry(db_session) -> None:
    from app.main import create_agent_runtime

    _executor, _recovery, _broker, engine_factory, _utility = create_agent_runtime()
    engine = engine_factory(
        db_session, "test-worker", channel_permissions=frozenset({"bilibili"})
    )

    # 渠道权限注入引擎。
    assert engine._channel_permissions == ("bilibili",)

    # 生产装配包含全部内部工具。
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    visible = await engine._registry.visible_tools(
        session_analyst, channel_permissions=engine._channel_permissions
    )
    names = {entry.internal_name for entry in visible}
    assert _INTERNAL_HISTORY | _INTERNAL_CALCULATION | _INTERNAL_ARTIFACT <= names
    assert "query_analysis_data" in names


# ---------------------------------------------------------------------------
# 7. 执行前实时复核目录行（G2）
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _shared_session(db_session):
    """与测试夹具共享同一会话的 session_factory（同连接内可见未提交行）。"""
    yield db_session


def _build_live_recheck_registry(db_session):
    return AgentToolRegistryFactory(
        session_factory=lambda: _shared_session(db_session)
    ).build(db_session)


async def _get_catalog_row(db_session, internal_name: str) -> McpToolCatalog:
    row = await db_session.scalar(
        select(McpToolCatalog).where(McpToolCatalog.internal_tool_name == internal_name)
    )
    assert row is not None
    return row


async def test_factory_execute_rejected_after_review_revoked(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    registry = _build_live_recheck_registry(db_session)
    await registry.visible_tools(session_analyst)  # 装配（缓存 approved 快照）

    # 装配后管理员隔离：执行路径实时复核必须拒绝。query_analysis_data 在审核
    # allowlist 内（挂了真实 AgentMcpTool），若复核缺失会跌入 prepare 抛
    # ValueError 而不是 UnknownToolError——该断言同时证明复核在预留之前。
    row = await _get_catalog_row(db_session, "query_analysis_data")
    row.review_status = "quarantined"
    await db_session.flush()

    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="query_analysis_data",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=session_analyst,
        )


async def test_factory_execute_rejected_after_disabled(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    registry = _build_live_recheck_registry(db_session)
    await registry.visible_tools(session_analyst)

    row = await _get_catalog_row(db_session, "query_analysis_data")
    row.is_enabled = False
    await db_session.flush()

    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="query_analysis_data",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=session_analyst,
        )


async def test_factory_execute_rejected_on_signature_drift(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    registry = _build_live_recheck_registry(db_session)
    await registry.visible_tools(session_analyst)

    row = await _get_catalog_row(db_session, "query_analysis_data")
    row.discovery_digest = "e" * 64
    await db_session.flush()

    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="query_analysis_data",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=session_analyst,
        )


async def test_factory_execute_rejected_when_catalog_row_deleted(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    registry = _build_live_recheck_registry(db_session)
    await registry.visible_tools(session_analyst)

    row = await _get_catalog_row(db_session, "query_analysis_data")
    await db_session.delete(row)
    await db_session.flush()

    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="query_analysis_data",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=session_analyst,
        )


async def test_factory_visible_tools_cached_while_execute_rechecks_live(db_session) -> None:
    # visible_tools 语义不变：仍用装配缓存；实时复核只加在执行路径。
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    registry = _build_live_recheck_registry(db_session)
    visible_before = await registry.visible_tools(session_analyst)
    assert "query_analysis_data" in {entry.internal_name for entry in visible_before}

    row = await _get_catalog_row(db_session, "query_analysis_data")
    row.review_status = "quarantined"
    await db_session.flush()

    visible_after = await registry.visible_tools(session_analyst)
    assert "query_analysis_data" in {entry.internal_name for entry in visible_after}

    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="query_analysis_data",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=session_analyst,
        )


async def test_factory_kol_detail_allowlist_stacks_with_live_recheck(db_session) -> None:
    await _add_catalog_row(
        db_session, internal_name="kol_detail", service_slug="social-grow-mcp"
    )
    await _add_catalog_row(db_session, internal_name="query_analysis_data")
    registry = _build_live_recheck_registry(db_session)
    await registry.visible_tools(kol_detail_profile)

    # allowlist 外：实时行健康也被 Profile 名单拒绝。
    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="query_analysis_data",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=kol_detail_profile,
        )

    # allowlist 内但事后被隔离：实时复核拒绝。
    row = await _get_catalog_row(db_session, "kol_detail")
    row.review_status = "quarantined"
    await db_session.flush()
    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="kol_detail",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile=kol_detail_profile,
        )
