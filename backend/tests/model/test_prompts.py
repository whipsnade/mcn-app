from app.model.prompts import (
    AGENT_LOOP_PROMPT,
    BRAINSTORM_PROMPT,
    BRAND_ANALYSIS_LOOP_PROMPT,
    BRAND_ANALYSIS_PROMPT,
    CAMPAIGN_ANALYSIS_LOOP_PROMPT,
    CAMPAIGN_ANALYSIS_PROMPT,
    CAMPAIGN_EVALUATE_PROMPT,
    FOLLOWUP_PROMPT,
    GOAL_PLANNER_PROMPT,
    PROMPTS,
    QUICK_AGENT_PROMPT,
    REPORT_WRITER_PROMPT,
    SUMMARY_PROMPT,
)


_ALL_PROMPTS = (
    SUMMARY_PROMPT,
    FOLLOWUP_PROMPT,
    AGENT_LOOP_PROMPT,
    REPORT_WRITER_PROMPT,
    BRAINSTORM_PROMPT,
    CAMPAIGN_EVALUATE_PROMPT,
    QUICK_AGENT_PROMPT,
    GOAL_PLANNER_PROMPT,
    BRAND_ANALYSIS_LOOP_PROMPT,
    CAMPAIGN_ANALYSIS_LOOP_PROMPT,
    BRAND_ANALYSIS_PROMPT,
    CAMPAIGN_ANALYSIS_PROMPT,
)


def test_prompts_treat_external_content_as_untrusted_and_limit_capabilities() -> None:
    for prompt in _ALL_PROMPTS:
        text = prompt.system
        assert "不可信数据" in text
        assert "只能使用传入" in text
        assert prompt.name.endswith("_v1")
        # goal_planner_v1 于 2026-07-30 升级契约（先澄清后执行 + available_tools），version=2。
        expected_version = "2" if prompt.name == "goal_planner_v1" else "1"
        assert prompt.version == expected_version
    for prompt in (SUMMARY_PROMPT, FOLLOWUP_PROMPT, AGENT_LOOP_PROMPT):
        assert "密钥" in prompt.system
        assert "URL" in prompt.system
    assert "目标 Schema" in AGENT_LOOP_PROMPT.system
    assert "不得编造" in AGENT_LOOP_PROMPT.system
    assert "export_contract" in AGENT_LOOP_PROMPT.system
    assert "required_field_names" in AGENT_LOOP_PROMPT.system
    assert "kol_detail" in AGENT_LOOP_PROMPT.system
    assert "某平台检索返回空即视为该平台已完成检索" in AGENT_LOOP_PROMPT.system
    assert "required_metrics" not in AGENT_LOOP_PROMPT.system
    assert "数据看板" in REPORT_WRITER_PROMPT.system
    assert "KOL 看板" in REPORT_WRITER_PROMPT.system


def test_goal_loop_prompts_are_registered_and_goal_specific() -> None:
    assert PROMPTS["brand_loop_v1"] is BRAND_ANALYSIS_LOOP_PROMPT
    assert PROMPTS["campaign_loop_v1"] is CAMPAIGN_ANALYSIS_LOOP_PROMPT
    brand = BRAND_ANALYSIS_LOOP_PROMPT.system
    assert "密钥" in brand and "URL" in brand
    assert "声量" in brand
    assert "情感" in brand
    assert "竞品" in brand
    assert "平台分布" in brand
    assert "内容主题" in brand
    assert "export_contract" not in brand
    assert "圈选名单" not in brand
    campaign = CAMPAIGN_ANALYSIS_LOOP_PROMPT.system
    assert "密钥" in campaign and "URL" in campaign
    assert "平台贡献" in campaign
    assert "达人贡献" in campaign
    assert "节奏" in campaign
    assert "复盘" in campaign
    assert "export_contract" not in campaign
    assert "圈选名单" not in campaign
    # 通用约束段与 AGENT_LOOP 一致（积分护栏/时间基准/空结果）。
    for text in (brand, campaign):
        assert "每次调用消耗 10 积分" in text
        assert "current_date" in text and "requested_period" in text
        assert "空结果即结论" in text
        assert "goal_params" in text


def test_goal_report_prompts_are_registered_and_structured() -> None:
    assert PROMPTS["brand_analysis_v1"] is BRAND_ANALYSIS_PROMPT
    assert PROMPTS["campaign_analysis_v1"] is CAMPAIGN_ANALYSIS_PROMPT
    brand = BRAND_ANALYSIS_PROMPT.system
    assert "不可信数据" in brand and "只能使用传入" in brand
    for block in ("metric_grid", "pie_chart", "line_chart", "tag_list", "markdown"):
        assert block in brand
    assert "声量" in brand
    assert "情感" in brand
    assert "内容主题" in brand
    assert "conclusion" in brand
    campaign = CAMPAIGN_ANALYSIS_PROMPT.system
    assert "不可信数据" in campaign and "只能使用传入" in campaign
    for block in ("metric_grid", "table", "line_chart", "markdown"):
        assert block in campaign
    assert "平台贡献" in campaign
    assert "达人贡献" in campaign
    assert "复盘" in campaign
    assert "conclusion" in campaign
    # 报告撰写器同样禁止编造。
    for text in (brand, campaign):
        assert "禁止编造" in text
        assert "目标 Schema" in text


def test_goal_summary_prompt_is_registered_and_constrained() -> None:
    from app.model.prompts import GOAL_SUMMARY_PROMPT

    assert PROMPTS["goal_summary_v1"] is GOAL_SUMMARY_PROMPT
    text = GOAL_SUMMARY_PROMPT.system
    assert "不可信数据" in text
    assert "只能使用传入" in text
    assert "summary" in text and "highlights" in text
    assert "brand_analysis" in text
    assert "campaign_analysis" in text
    assert "kol_selection" in text


def test_prompts_do_not_contain_provider_endpoints_or_environment_values(monkeypatch) -> None:
    secret = "unit-test-secret-never-in-prompt"
    endpoint = "https://datatap.example.invalid/private"
    monkeypatch.setenv("DATATAP_MCP_TOKEN", secret)
    monkeypatch.setenv("DATATAP_MCP_BASE_URL", endpoint)

    combined = "\n".join(prompt.system for prompt in _ALL_PROMPTS)

    assert secret not in combined
    assert endpoint not in combined
    assert "api/gateway" not in combined
    assert "TENCENT_PLAN_API_KEY" not in combined
