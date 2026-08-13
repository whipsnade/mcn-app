"""Agent Profile 注册表（设计文档 §五）。

Profile 只限定 Agent 的能力边界：允许动作、输出 Schema、最大上下文预算、
system prompt 引用。**Profile 不包含任何业务调用顺序或固定阶段清单**——
这是结构性保证：字段集合即唯一能力描述载体，见 test_profiles.py
对字段名的断言。

直接发布协议：新执行路径不再使用模型 Reviewer，所有在役 Profile 的
``requires_reviewer`` 恒为 False；字段与 ``artifact_reviewer_v1`` 注册仅
保留供历史代码导入。
"""

from dataclasses import dataclass

from app.agent_runtime.schemas import FOUR_ACTIONS

# 工具分类标签词汇表（设计文档 §五「允许工具集合」）。
# 分类是 CATEGORY TAGS 而非具体工具名：具体工具在 Task 7 Tool Registry 中
# 按分类解析。引擎据此对 call_tool.internal_tool_name 做按 Profile 的校验。
MCP_TOOLS = "mcp"  # 已审核 DataTap MCP 工具
HISTORY_TOOLS = "history"  # read_artifact / search_evidence / read_tool_result
CALCULATION_TOOLS = "calculation"  # calculate_expression / aggregate_metrics / rank_kols
ARTIFACT_TOOLS = "artifact"  # Artifact Draft 创建 / 更新 / 提交工具
KOL_DETAIL_TOOLS = "kol_detail"  # KOL 详情 / 原帖 / 只读缓存工具

TOOL_CATEGORIES: frozenset[str] = frozenset(
    {MCP_TOOLS, HISTORY_TOOLS, CALCULATION_TOOLS, ARTIFACT_TOOLS, KOL_DETAIL_TOOLS}
)

# kol_detail_v1 可用的 MCP 工具明确名单（设计 §5.1）：达人详情 + 原帖/热帖查询。
# 名字对齐 mcp_gateway.registry.DYNAMIC_TOOL_ALLOWLIST 的审核内部名：
# - kol_detail（social-grow-mcp）：指定平台达人详情与趋势画像；
# - query_raw_posts（insight-cube-mcp）：社媒原帖明细检索。
KOL_DETAIL_MCP_TOOL_ALLOWLIST: frozenset[str] = frozenset({"kol_detail", "query_raw_posts"})


@dataclass(frozen=True)
class AgentProfile:
    """一个冻结的 Agent 能力配置。

    ``allowed_actions`` 必须是四种动作协议（schemas.FOUR_ACTIONS）的子集。
    ``allowed_tool_categories`` 必须是 TOOL_CATEGORIES 词汇表的子集，且是集合
    而非有序序列——Profile 仍不编码任何固定工具调用顺序。
    ``mcp_tool_allowlist`` 是可选的 MCP 内部工具名明确名单：为 None 时整个
    MCP_TOOLS 分类按审核/渠道过滤放行；非 None 时 Registry 只放行名单内的
    MCP 工具（设计 §5.1，kol_detail_v1 的达人详情/热帖名单）。
    ``output_schema`` 是短描述符：``agent_actions``（四种动作协议）/
    ``review_decision``（approve/revise/reject，独立于动作协议；遗留，
    仅 artifact_reviewer_v1 使用）/ ``utility_json``（对应强类型 Utility 输出）。
    ``allowed_artifact_contracts`` 是该 Profile 的审核输出契约 allowlist；它
    只描述可以交付的已审核产物类型，不描述 Builder 调用顺序。Run 创建时
    RuntimeConfigService 要求 runtime config 的 profile 映射同时命中此 allowlist
    和当前 capability pack 的 typed contract。
    """

    name: str
    version: str
    allowed_actions: frozenset[str]
    allowed_tool_categories: frozenset[str]
    requires_reviewer: bool
    max_context_budget: int
    output_schema: str
    system_prompt_key: str
    allowed_artifact_contracts: frozenset[str] = frozenset()
    mcp_tool_allowlist: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.allowed_actions.issubset(FOUR_ACTIONS):
            raise ValueError(
                f"allowed_actions must be a subset of {sorted(FOUR_ACTIONS)}; "
                f"got {sorted(self.allowed_actions)}"
            )
        if not self.allowed_tool_categories.issubset(TOOL_CATEGORIES):
            raise ValueError(
                f"allowed_tool_categories must be a subset of {sorted(TOOL_CATEGORIES)}; "
                f"got {sorted(self.allowed_tool_categories)}"
            )

    @property
    def full_name(self) -> str:
        """注册表键，如 ``session_analyst_v1``。"""
        return f"{self.name}_{self.version}"


def _make_profile(
    name: str,
    version: str,
    allowed_actions: frozenset[str],
    allowed_tool_categories: frozenset[str],
    requires_reviewer: bool,
    max_context_budget: int,
    output_schema: str,
    allowed_artifact_contracts: frozenset[str] = frozenset(),
    mcp_tool_allowlist: frozenset[str] | None = None,
) -> AgentProfile:
    return AgentProfile(
        name=name,
        version=version,
        allowed_actions=allowed_actions,
        allowed_tool_categories=allowed_tool_categories,
        requires_reviewer=requires_reviewer,
        max_context_budget=max_context_budget,
        output_schema=output_schema,
        system_prompt_key=f"{name}_{version}",
        allowed_artifact_contracts=allowed_artifact_contracts,
        mcp_tool_allowlist=mcp_tool_allowlist,
    )


PROFILES: dict[str, AgentProfile] = {
    profile.full_name: profile
    for profile in [
        # 所有普通会话消息的入口：全部四种动作，正式产物由 publish_artifacts
        # 直接发布（确定性发布服务校验，无模型 Reviewer）。
        _make_profile(
            name="session_analyst",
            version="v1",
            allowed_actions=frozenset({"ask_user", "call_tool", "publish_artifacts", "complete"}),
            allowed_tool_categories=frozenset(
                {MCP_TOOLS, HISTORY_TOOLS, CALCULATION_TOOLS, ARTIFACT_TOOLS}
            ),
            requires_reviewer=False,
            max_context_budget=128_000,
            output_schema="agent_actions",
            allowed_artifact_contracts=frozenset(
                {
                    "brand_report_v3",
                    "campaign_report_v3",
                    "kol_selection_v3",
                    "insight_board_v1",
                }
            ),
        ),
        # （遗留）正式 Artifact 提交复核：只读，输出 approve/revise/reject，
        # 禁用工具。Reviewer 已从新执行路径下线，本 Profile 仅保留供历史
        # 代码导入，不得出现在新 Runtime wiring 中。
        _make_profile(
            name="artifact_reviewer",
            version="v1",
            allowed_actions=frozenset(),
            allowed_tool_categories=frozenset(),
            requires_reviewer=False,
            max_context_budget=32_000,
            output_schema="review_decision",
        ),
        # 点击圈选达人的轻量 Run：缓存未命中时经明确 allowlist 的 MCP 工具
        # （KOL_DETAIL_MCP_TOOL_ALLOWLIST：达人详情 kol_detail + 原帖/热帖
        # query_raw_posts，非整个 MCP_TOOLS 分类）抓取真实数据，构建
        # kol_detail_v2 后由 publish_artifacts 直接发布；KOL_DETAIL_TOOLS
        # 分类保留给只读缓存/详情内部工具。点击触发的无澄清交互，故不允许
        # ask_user。
        _make_profile(
            name="kol_detail",
            version="v1",
            allowed_actions=frozenset({"call_tool", "publish_artifacts", "complete"}),
            allowed_tool_categories=frozenset({MCP_TOOLS, KOL_DETAIL_TOOLS, ARTIFACT_TOOLS}),
            requires_reviewer=False,
            max_context_budget=32_000,
            output_schema="agent_actions",
            allowed_artifact_contracts=frozenset({"insight_board_v1"}),
            mcp_tool_allowlist=KOL_DETAIL_MCP_TOOL_ALLOWLIST,
        ),
        # 标题、Run 摘要、建议等后台轻量任务：只输出受控结构，不需要 Reviewer。
        _make_profile(
            name="utility",
            version="v1",
            allowed_actions=frozenset({"complete"}),
            allowed_tool_categories=frozenset(),
            requires_reviewer=False,
            max_context_budget=8_000,
            output_schema="utility_json",
        ),
    ]
}


def get_profile(name: str) -> AgentProfile:
    """按全名（如 ``session_analyst_v1``）查找 Profile；未注册抛出 KeyError。"""
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown agent profile: {name!r}") from None


__all__ = [
    "ARTIFACT_TOOLS",
    "AgentProfile",
    "CALCULATION_TOOLS",
    "HISTORY_TOOLS",
    "KOL_DETAIL_MCP_TOOL_ALLOWLIST",
    "KOL_DETAIL_TOOLS",
    "MCP_TOOLS",
    "PROFILES",
    "TOOL_CATEGORIES",
    "get_profile",
]
