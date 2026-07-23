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
