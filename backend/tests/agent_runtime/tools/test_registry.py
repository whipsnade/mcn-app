"""统一 Tool Registry 与 Profile 权限测试（设计文档 §十 / §16）。

覆盖：TrustedTool 契约与统一 ToolResult 形状、注册与去重、服务端上下文
不可被模型参数覆盖（§16）、按 Profile 分类过滤、MCP 审核状态过滤、用户
渠道权限过滤、ToolContext 传播。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent_runtime.profiles import (
    ARTIFACT_TOOLS,
    CALCULATION_TOOLS,
    HISTORY_TOOLS,
    KOL_DETAIL_TOOLS,
    MCP_TOOLS,
    PROFILES,
)
from app.agent_runtime.tools.contracts import (
    SERVER_RESERVED_KEYS,
    ToolContext,
    ToolResult,
)
from app.agent_runtime.tools.registry import (
    McpCatalogEntry,
    RegisteredTool,
    ToolContractError,
    ToolRegistry,
    UnknownToolError,
)
from app.identity.models import UserChannelPermission
from app.mcp_gateway.models import McpToolCatalog

session_analyst = PROFILES["session_analyst_v1"]
kol_detail = PROFILES["kol_detail_v1"]
artifact_reviewer = PROFILES["artifact_reviewer_v1"]
utility = PROFILES["utility_v1"]


class FakeCalcArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1)


class FakeTool:
    """一个符合 TrustedTool 契约的假工具：记录收到的上下文与参数。"""

    input_model = FakeCalcArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, name: str, **result: Any) -> None:
        self.name = name
        self.calls: list[tuple[ToolContext, BaseModel]] = []
        self._result = result

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        self.calls.append((context, arguments))
        return ToolResult(status="success", safe_summary=f"{self.name} ok", **self._result)


def _catalog_entry(
    *,
    internal_name: str,
    service_slug: str = "insight-cube-mcp",
    review_status: str = "approved",
    is_enabled: bool = True,
) -> McpCatalogEntry:
    return McpCatalogEntry(
        internal_tool_name=internal_name,
        service_slug=service_slug,
        reviewed_description=f"{internal_name} 描述",
        input_schema_json={"type": "object"},
        review_status=review_status,
        is_enabled=is_enabled,
    )


async def _db_catalog_source(db_session) -> list[McpToolCatalog]:
    rows = (await db_session.scalars(select(McpToolCatalog))).all()
    return list(rows)


async def _add_catalog_row(
    db_session,
    *,
    internal_name: str,
    service_slug: str = "bilibili-mcp",
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
# 1. TrustedTool 契约与统一 ToolResult 形状
# ---------------------------------------------------------------------------


def test_tool_result_unified_shape() -> None:
    result = ToolResult(
        status="success",
        safe_summary="1 + 1 = 2",
        evidence_id="ev-1",
        cursor="next",
        truncated=True,
        error_type=None,
    )
    assert result.status == "success"
    assert result.safe_summary == "1 + 1 = 2"
    assert result.evidence_id == "ev-1"
    assert result.cursor == "next"
    assert result.truncated is True
    assert result.error_type is None

    default = ToolResult(status="unknown", safe_summary="")
    assert default.evidence_id is None
    assert default.cursor is None
    assert default.truncated is False
    assert default.error_type is None


def test_valid_tool_registers() -> None:
    registry = ToolRegistry()
    entry = registry.register(FakeTool("calculate_expression"), category=CALCULATION_TOOLS)
    assert isinstance(entry, RegisteredTool)
    assert entry.internal_name == "calculate_expression"
    assert entry.category == CALCULATION_TOOLS
    assert entry.points_cost == 0
    assert entry.external_side_effect is False


def test_protocol_violation_rejected_at_registration() -> None:
    registry = ToolRegistry()

    class NoName:
        input_model = FakeCalcArgs
        points_cost = 0
        external_side_effect = False

        async def execute(self, context, arguments): ...  # pragma: no cover

    with pytest.raises(ToolContractError):
        registry.register(NoName(), category=CALCULATION_TOOLS)

    class BadInputModel:
        name = "bad_input_model"
        input_model = dict  # type: ignore[assignment]
        points_cost = 0
        external_side_effect = False

        async def execute(self, context, arguments): ...  # pragma: no cover

    with pytest.raises(ToolContractError):
        registry.register(BadInputModel(), category=CALCULATION_TOOLS)

    class NoExecute:
        name = "no_execute"
        input_model = FakeCalcArgs
        points_cost = 0
        external_side_effect = False

    with pytest.raises(ToolContractError):
        registry.register(NoExecute(), category=CALCULATION_TOOLS)

    class NonIntCost:
        name = "non_int_cost"
        input_model = FakeCalcArgs
        points_cost = "zero"  # type: ignore[assignment]
        external_side_effect = False

        async def execute(self, context, arguments): ...  # pragma: no cover

    with pytest.raises(ToolContractError):
        registry.register(NonIntCost(), category=CALCULATION_TOOLS)


def test_negative_points_cost_rejected() -> None:
    class NegativeCost:
        name = "negative_cost"
        input_model = FakeCalcArgs
        points_cost = -1
        external_side_effect = True

        async def execute(self, context, arguments): ...  # pragma: no cover

    registry = ToolRegistry()
    with pytest.raises(ToolContractError):
        registry.register(NegativeCost(), category=CALCULATION_TOOLS)


def test_zero_points_cost_is_allowed() -> None:
    registry = ToolRegistry()
    entry = registry.register(FakeTool("calculate_expression"), category=CALCULATION_TOOLS)
    assert entry.points_cost == 0


def test_tool_input_model_cannot_declare_server_reserved_keys() -> None:
    class ReservedArgs(BaseModel):
        user_id: str

    class DeclaresReserved:
        name = "declares_reserved"
        input_model = ReservedArgs
        points_cost = 0
        external_side_effect = False

        async def execute(self, context, arguments): ...  # pragma: no cover

    registry = ToolRegistry()
    with pytest.raises(ToolContractError):
        registry.register(DeclaresReserved(), category=CALCULATION_TOOLS)


# ---------------------------------------------------------------------------
# 2. 注册与去重
# ---------------------------------------------------------------------------


def test_duplicate_internal_name_rejected() -> None:
    registry = ToolRegistry()
    registry.register(FakeTool("calculate_expression"), category=CALCULATION_TOOLS)
    with pytest.raises(ToolContractError):
        registry.register(FakeTool("calculate_expression"), category=CALCULATION_TOOLS)


def test_register_rejects_unknown_and_mcp_category() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolContractError):
        registry.register(FakeTool("typo_tool"), category="typo_category")
    # MCP 工具一律来自审核目录快照，不允许手动 register。
    with pytest.raises(ToolContractError):
        registry.register(FakeTool("general_search"), category=MCP_TOOLS)


def test_registry_exposes_registered_set() -> None:
    registry = ToolRegistry()
    registry.register(FakeTool("read_artifact"), category=HISTORY_TOOLS)
    registry.register(FakeTool("calculate_expression"), category=CALCULATION_TOOLS)
    registry.register(FakeTool("create_artifact"), category=ARTIFACT_TOOLS)
    names = {entry.internal_name for entry in registry.registered_tools}
    assert names == {"read_artifact", "calculate_expression", "create_artifact"}


# ---------------------------------------------------------------------------
# 3. 模型参数不能覆盖服务端上下文
# ---------------------------------------------------------------------------


async def test_model_arguments_cannot_override_server_context() -> None:
    registry = ToolRegistry()
    tool = FakeTool("calculate_expression")
    registry.register(tool, category=CALCULATION_TOOLS)

    result = await registry.execute(
        internal_name="calculate_expression",
        arguments={
            "expression": "1 + 1",
            "user_id": "attacker",
            "session_id": "attacker-session",
            "run_id": "attacker-run",
        },
        user_id="server-user",
        session_id="server-session",
        run_id="server-run",
        profile_name="session_analyst_v1",
    )

    assert result.status == "success"
    context, parsed = tool.calls[0]
    # 服务端上下文是注入值，不是模型值。
    assert context.user_id == "server-user"
    assert context.session_id == "server-session"
    assert context.run_id == "server-run"
    assert context.profile_name == "session_analyst_v1"
    # 模型参数中的保留键在进入 input_model 校验前被剥离。
    assert parsed.expression == "1 + 1"
    assert SERVER_RESERVED_KEYS.isdisjoint(parsed.model_dump())


async def test_execute_unknown_tool_raises() -> None:
    registry = ToolRegistry()
    with pytest.raises(UnknownToolError):
        await registry.execute(
            internal_name="nope",
            arguments={},
            user_id="u",
            session_id="s",
            run_id="r",
            profile_name="session_analyst_v1",
        )


# ---------------------------------------------------------------------------
# 4. 按 Profile 分类过滤
# ---------------------------------------------------------------------------


async def test_profile_category_filtering() -> None:
    registry = ToolRegistry(
        catalog_source=[_catalog_entry(internal_name="query_analysis_data")]
    )
    registry.register(FakeTool("read_artifact"), category=HISTORY_TOOLS)
    registry.register(FakeTool("calculate_expression"), category=CALCULATION_TOOLS)
    registry.register(FakeTool("create_artifact"), category=ARTIFACT_TOOLS)
    registry.register(FakeTool("kol_detail"), category=KOL_DETAIL_TOOLS)

    analyst_visible = await registry.visible_tools(session_analyst)
    assert {entry.internal_name for entry in analyst_visible} == {
        "query_analysis_data",  # mcp
        "read_artifact",  # history
        "calculate_expression",  # calculation
        "create_artifact",  # artifact
    }

    kol_visible = await registry.visible_tools(kol_detail)
    assert {entry.internal_name for entry in kol_visible} == {"kol_detail", "create_artifact"}

    for profile in (artifact_reviewer, utility):
        assert await registry.visible_tools(profile) == ()


# ---------------------------------------------------------------------------
# 5. MCP 工具：仅审核通过且启用
# ---------------------------------------------------------------------------


async def test_mcp_requires_approved_and_enabled(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="general_search")
    await _add_catalog_row(
        db_session, internal_name="disabled_search", review_status="approved", is_enabled=False
    )
    await _add_catalog_row(
        db_session, internal_name="quarantined_search", review_status="quarantined", is_enabled=True
    )

    registry = ToolRegistry(catalog_source=lambda: _db_catalog_source(db_session))
    visible = await registry.visible_tools(session_analyst, channel_permissions={"bilibili"})
    names = {entry.internal_name for entry in visible}
    assert names == {"general_search"}
    assert "disabled_search" not in names
    assert "quarantined_search" not in names


async def test_mcp_approved_enabled_invisible_to_reviewer_and_utility(db_session) -> None:
    await _add_catalog_row(db_session, internal_name="general_search")

    registry = ToolRegistry(catalog_source=lambda: _db_catalog_source(db_session))
    for profile in (artifact_reviewer, utility):
        visible = await registry.visible_tools(profile, channel_permissions={"bilibili"})
        assert visible == ()


# ---------------------------------------------------------------------------
# 6. 用户渠道权限过滤
# ---------------------------------------------------------------------------


async def test_mcp_tool_requires_user_channel_permission(db_session, user_factory) -> None:
    await _add_catalog_row(db_session, internal_name="general_search")

    user = await user_factory()
    user_id = user.id
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        UserChannelPermission(
            id=str(uuid4()),
            user_id=user_id,
            channel="xiaohongshu",  # 用户只有小红书渠道，没有 B站
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()

    granted = frozenset(
        row.channel
        for row in (
            await db_session.scalars(
                select(UserChannelPermission).where(
                    UserChannelPermission.user_id == user_id,
                    UserChannelPermission.is_enabled.is_(True),
                )
            )
        ).all()
    )

    registry = ToolRegistry(catalog_source=lambda: _db_catalog_source(db_session))
    visible = await registry.visible_tools(session_analyst, channel_permissions=granted)
    assert "general_search" not in {entry.internal_name for entry in visible}

    # 补授 B站渠道后可见。
    db_session.add(
        UserChannelPermission(
            id=str(uuid4()),
            user_id=user_id,
            channel="bilibili",
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()
    granted_with_bilibili = granted | {"bilibili"}
    visible2 = await registry.visible_tools(
        session_analyst, channel_permissions=granted_with_bilibili
    )
    assert "general_search" in {entry.internal_name for entry in visible2}


# ---------------------------------------------------------------------------
# 7. ToolContext 传播
# ---------------------------------------------------------------------------


async def test_tool_context_propagates_server_context() -> None:
    registry = ToolRegistry()
    tool = FakeTool("calculate_expression")
    registry.register(tool, category=CALCULATION_TOOLS)

    await registry.execute(
        internal_name="calculate_expression",
        arguments={"expression": "2 * 2"},
        user_id="u-1",
        session_id="s-1",
        run_id="r-1",
        profile_name="session_analyst_v1",
    )

    context, parsed = tool.calls[0]
    assert context == ToolContext(
        user_id="u-1",
        session_id="s-1",
        run_id="r-1",
        profile_name="session_analyst_v1",
    )
    assert parsed.expression == "2 * 2"


async def test_failed_result_propagates_error_type() -> None:
    class FailingTool(FakeTool):
        async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
            self.calls.append((context, arguments))
            return ToolResult(
                status="failed",
                safe_summary="调用失败",
                error_type="definitely_not_sent",
            )

    registry = ToolRegistry()
    tool = FailingTool("rank_kols")
    registry.register(tool, category=CALCULATION_TOOLS)

    result = await registry.execute(
        internal_name="rank_kols",
        arguments={"expression": "1"},
        user_id="u",
        session_id="s",
        run_id="r",
        profile_name="session_analyst_v1",
    )
    assert result.status == "failed"
    assert result.error_type == "definitely_not_sent"
    assert result.evidence_id is None
