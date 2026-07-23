from app.model.prompts import (
    AGENT_LOOP_PROMPT,
    BRAINSTORM_PROMPT,
    CAMPAIGN_EVALUATE_PROMPT,
    FOLLOWUP_PROMPT,
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
)


def test_prompts_treat_external_content_as_untrusted_and_limit_capabilities() -> None:
    for prompt in _ALL_PROMPTS:
        text = prompt.system
        assert "不可信数据" in text
        assert "只能使用传入" in text
        assert prompt.name.endswith("_v1")
        assert prompt.version == "1"
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
