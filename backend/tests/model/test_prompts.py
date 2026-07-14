from app.model.prompts import ANALYST_PROMPT, PLANNER_PROMPT, SUMMARY_PROMPT


def test_prompts_treat_external_content_as_untrusted_and_limit_capabilities() -> None:
    for prompt in (PLANNER_PROMPT, ANALYST_PROMPT, SUMMARY_PROMPT):
        text = prompt.system
        assert "不可信数据" in text
        assert "只能使用传入" in text
        assert "密钥" in text
        assert "URL" in text
        assert prompt.name.endswith("_v1")
        assert prompt.version == "1"
    assert "目标 Schema" in PLANNER_PROMPT.system


def test_prompts_do_not_contain_provider_endpoints_or_environment_values(monkeypatch) -> None:
    secret = "unit-test-secret-never-in-prompt"
    endpoint = "https://datatap.example.invalid/private"
    monkeypatch.setenv("DATATAP_MCP_TOKEN", secret)
    monkeypatch.setenv("DATATAP_MCP_BASE_URL", endpoint)

    combined = "\n".join(
        prompt.system for prompt in (PLANNER_PROMPT, ANALYST_PROMPT, SUMMARY_PROMPT)
    )

    assert secret not in combined
    assert endpoint not in combined
    assert "api/gateway" not in combined
    assert "TENCENT_PLAN_API_KEY" not in combined
