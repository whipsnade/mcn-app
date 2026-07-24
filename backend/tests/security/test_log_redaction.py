from __future__ import annotations

import json

from app.core.redaction import redact_for_log


def test_redaction_removes_nested_auth_phone_and_supplier_tokens() -> None:
    value = {
        "phone": "13812345678",
        "request": {
            "Authorization": "Bearer unit-test-authorization-value",
            "cookie": "session=unit-test-cookie-value",
        },
        "providers": [
            {"tencent_plan_api_key": "unit-test-model-value"},
            {"datatap_mcp_token": "unit-test-mcp-value"},
            {"jwt_secret": "unit-test-jwt-secret"},
            {"mysql_password": "unit-test-db-password"},
        ],
        "message": "联系 13912345678 继续处理",
    }

    serialized = json.dumps(redact_for_log(value), ensure_ascii=False)
    for secret in (
        "13812345678",
        "13912345678",
        "unit-test-authorization-value",
        "unit-test-cookie-value",
        "unit-test-model-value",
        "unit-test-mcp-value",
        "unit-test-jwt-secret",
        "unit-test-db-password",
    ):
        assert secret not in serialized


def test_redaction_preserves_non_sensitive_shape() -> None:
    rendered = redact_for_log({"count": 2, "items": ["ok", {"name": "达人"}]})
    assert rendered == {"count": 2, "items": ["ok", {"name": "达人"}]}


def test_redaction_masks_env_style_assignments_without_overmatching_business_text() -> None:
    value = (
        "api_key=short-model-value token=short-mcp-value "
        "TENCENT_PLAN_API_KEY=env-model-value "
        "DATATAP_MCP_TOKEN=env-mcp-value "
        "品牌 token 化传播策略保持不变"
    )

    rendered = redact_for_log(value)

    for secret in (
        "short-model-value",
        "short-mcp-value",
        "env-model-value",
        "env-mcp-value",
    ):
        assert secret not in rendered
    assert "品牌 token 化传播策略保持不变" in rendered


def test_redaction_masks_quoted_json_credentials_without_overmatching_business_text() -> None:
    value = (
        '{"token":"quoted-token-value","api_key": "quoted-api-key-value",'
        '"campaign":"品牌 token 化传播策略","note":"api key 视觉主题"}'
    )

    rendered = redact_for_log(value)

    assert "quoted-token-value" not in rendered
    assert "quoted-api-key-value" not in rendered
    assert "品牌 token 化传播策略" in rendered
    assert "api key 视觉主题" in rendered


def test_redaction_consumes_escaped_json_credential_values_completely() -> None:
    value = json.dumps(
        {
            "password": 'password-prefix"password-tail\\password-end',
            "token": 'token-prefix"token-tail\\token-end',
            "api_key": 'key-prefix"key-tail\\key-end',
            "campaign": "品牌 token 化传播策略",
        },
        ensure_ascii=False,
    )

    rendered = redact_for_log(value)

    for leaked_suffix in (
        "password-tail",
        "password-end",
        "token-tail",
        "token-end",
        "key-tail",
        "key-end",
    ):
        assert leaked_suffix not in rendered
    assert "品牌 token 化传播策略" in rendered
