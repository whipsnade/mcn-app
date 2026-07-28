from app.model.prompts import BRAND_ANALYSIS_LOOP_PROMPT, GOAL_PLANNER_PROMPT, PROMPTS


def test_goal_planner_prompt_enforces_business_boundaries() -> None:
    text = GOAL_PLANNER_PROMPT.system
    assert GOAL_PLANNER_PROMPT.name == "goal_planner_v1"
    assert GOAL_PLANNER_PROMPT.version == "1"
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
    assert PROMPTS["goal_planner_v1"] is GOAL_PLANNER_PROMPT


def test_brand_loop_prompt_declares_tool_call_contract() -> None:
    text = BRAND_ANALYSIS_LOOP_PROMPT.system

    assert BRAND_ANALYSIS_LOOP_PROMPT.name == "brand_loop_v1"
    # internal_tool_name 是顶层必填字段，禁止嵌进 arguments。
    assert "internal_tool_name 是 call_tool 决策的顶层必填字段" in text
    assert "禁止嵌进 arguments" in text
    # 只有能给出完整工具名时才输出 call_tool；不确定时 finish 并说明证据不足。
    assert "完整的 internal_tool_name 时才输出 call_tool" in text
    assert "证据不足" in text
    assert "不得输出空工具调用" in text
    # 阶段工具顺序提示：①品牌标签匹配 → ②概览 → ③趋势 → ④可选话题/受众。
    assert "①品牌标签匹配" in text
    assert "②整体概览" in text
    assert "③趋势分析" in text
    assert "④可选的热门话题与受众画像" in text
    # 「趋势分析」优先调用 social.statistic.trend。
    assert "趋势分析" in text
    assert "social.statistic.trend" in text
    # 循环状态注入说明：called_tools / evidence_gaps。
    assert "called_tools" in text
    assert "evidence_gaps" in text
