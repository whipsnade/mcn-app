"""Brainstorm 澄清关键字表：从 MCP 工具入参要求提炼的参数需求清单。

静态契约，注入 BRAINSTORM prompt 驱动模型逐项提炼用户输入；
ready 判定以必填项齐全为准（kol_filters 仅在达人筛选意图时必填）。
"""

BRAINSTORM_PARAMETERS: tuple[dict[str, str], ...] = (
    {
        "key": "brand",
        "label": "分析主体",
        "description": "分析主体品牌/对象名",
        "required": "必填",
        "mcp_mapping": "统计工具 name / anys",
    },
    {
        "key": "category",
        "label": "行业品类",
        "description": "行业品类（一级品类清单）",
        "required": "必填",
        "mcp_mapping": "品类分析 name",
    },
    {
        "key": "platforms",
        "label": "渠道",
        "description": "渠道子集（小红书/抖音/B站/微博/微信）",
        "required": "必填",
        "mcp_mapping": "datasource",
    },
    {
        "key": "audience",
        "label": "受众人群",
        "description": "受众人群（年龄/性别/地域）",
        "required": "可选",
        "mcp_mapping": "画像查询与筛选过滤",
    },
    {
        "key": "period",
        "label": "统计时间窗",
        "description": "统计时间窗（缺省近 3 个月）",
        "required": "可选",
        "mcp_mapping": "start_time/end_time",
    },
    {
        "key": "kol_filters",
        "label": "达人筛选",
        "description": "达人筛选（粉丝量门槛/达人类型/价格带）",
        "required": "KOL 意图时必填",
        "mcp_mapping": "hot.user/kol.search filters",
    },
    {
        "key": "goal",
        "label": "分析目标",
        "description": "分析目标（声量口碑/达人投放/竞品对比…）",
        "required": "必填",
        "mcp_mapping": "指导工具选择与报告侧重",
    },
    {
        "key": "region",
        "label": "目标地区",
        "description": "目标地区（达人粉丝地域口径，如杭州/上海）",
        "required": "可选",
        "mcp_mapping": "受众地域过滤",
    },
)

__all__ = ["BRAINSTORM_PARAMETERS"]
