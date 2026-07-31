from app.model.prompts import (
    BRAND_ANALYSIS_LOOP_PROMPT,
    BRAND_REPORT_NARRATIVE_PROMPT,
    GOAL_PLANNER_PROMPT,
    PROMPTS,
)


def test_goal_planner_prompt_enforces_business_boundaries() -> None:
    text = GOAL_PLANNER_PROMPT.system
    assert GOAL_PLANNER_PROMPT.name == "goal_planner_v1"
    assert GOAL_PLANNER_PROMPT.version == "3"
    assert "brand_analysis" in text
    assert "campaign_analysis" in text
    assert "kol_selection" in text
    assert "活动必须属于品牌" in text
    assert "明确要求圈选" in text
    assert "request_evidence" in text
    assert "不得调用工具" in text
    assert "action=respond" in text
    assert "respond_type=context_qa" in text
    assert "respond_type=usage_help" in text
    assert "respond_type=out_of_scope" in text
    assert "respond_type 必须为 null" in text
    assert "不可信数据" in text
    assert "exemplar 只用于参考匿名结构" in text
    assert "不得复制其中的实体" in text
    # 日期锚点规则：相对时间以 current_date 为基准折算。
    assert "current_date" in text
    # 先澄清后执行：分析类意图首轮必须 clarify，围绕 available_tools 能力追问；
    # 澄清轮次由模型判断，可多轮直到条件足够。
    assert "available_tools" in text
    assert "先澄清后执行" in text
    assert "澄清轮次由你判断" in text
    assert "不得重复追问用户已回答过的条件" in text
    assert "直接执行" in text
    # comparison_mode 落参规则：仅 brand_analysis 使用，默认 mom，明确要求同比时 mom_yoy。
    assert "comparison_mode" in text
    assert "mom_yoy" in text
    assert "其余 brand_analysis 一律 mom" in text
    assert "campaign_analysis 与 kol_selection 的 params 不得输出该字段" in text
    assert PROMPTS["goal_planner_v1"] is GOAL_PLANNER_PROMPT


def test_brand_loop_prompt_declares_tool_call_contract() -> None:
    text = BRAND_ANALYSIS_LOOP_PROMPT.system

    assert BRAND_ANALYSIS_LOOP_PROMPT.name == "brand_loop_v1"
    # brand_loop_v1 于 2026-07-31 升级 v3：失败不得整体收尾、趋势单平台查询
    # （v2：对比期阶段与期别标注）。
    assert BRAND_ANALYSIS_LOOP_PROMPT.version == "3"
    # 对比期由 comparison_mode 与 period 决定；无有效 period 时不得猜测对比窗。
    assert "comparison_mode" in text
    assert "mom_yoy" in text
    assert "2 月 28 日" in text
    assert "不得猜测对比窗" in text
    # evidence_goal 必须以期别前缀标注该调用属于哪个期别。
    assert "current:" in text
    assert "mom:" in text
    assert "yoy:" in text
    # 期别无关的调用（如标签匹配）标注 current:。
    assert "期别无关" in text
    # 工具失败不得整体收尾；趋势按单平台逐个调用。
    assert "不得触发整体收尾" in text
    assert "datasource 每次只传一个平台" in text
    # internal_tool_name 是顶层必填字段，禁止嵌进 arguments。
    assert "internal_tool_name 是 call_tool 决策的顶层必填字段" in text
    assert "禁止嵌进 arguments" in text
    # 只有能给出完整工具名时才输出 call_tool；不确定时 finish 并说明证据不足。
    assert "完整的 internal_tool_name 时才输出 call_tool" in text
    assert "证据不足" in text
    assert "不得输出空工具调用" in text
    # 阶段工具顺序提示：①品牌标签匹配 → ②概览 → ③对比期 → ④趋势 → ⑤情感/话题/受众/热帖。
    assert "①品牌标签匹配" in text
    assert "②整体概览" in text
    assert "③对比期" in text
    assert "④趋势分析" in text
    assert "⑤情感明细/热门话题/受众画像/热门帖子" in text
    # 「趋势分析」优先调用 social.statistic.trend。
    assert "趋势分析" in text
    assert "social.statistic.trend" in text
    # 循环状态注入说明：called_tools / evidence_gaps。
    assert "called_tools" in text
    assert "evidence_gaps" in text


def test_brand_report_narrative_prompt_is_registered_and_constrained() -> None:
    text = BRAND_REPORT_NARRATIVE_PROMPT.system

    assert BRAND_REPORT_NARRATIVE_PROMPT.name == "brand_report_narrative_v1"
    assert BRAND_REPORT_NARRATIVE_PROMPT.version == "1"
    assert PROMPTS["brand_report_narrative_v1"] is BRAND_REPORT_NARRATIVE_PROMPT
    # 通用约束锚点（与 tests/model/test_prompts.py 全 prompt 循环一致）。
    assert "不可信数据" in text
    assert "只能使用传入" in text
    assert "目标 Schema" in text
    # 数据纪律：只能引用传入 data，禁止创造/换算/修改指标。
    assert "只能引用传入 data" in text
    assert "禁止创造、换算或修改任何指标" in text
    # 降级与对比期护栏。
    assert "availability 非 complete" in text
    assert "对比期 status 非 ok" in text
    # 输出字段契约。
    for field in (
        "praise_points",
        "complaint_points",
        "impact_level",
        "expansion_signals",
        "noise_notes",
        "key_findings",
        "conclusion",
        "recommendations",
    ):
        assert field in text
