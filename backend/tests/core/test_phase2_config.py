import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.mcp_gateway.contracts import DataTapService


def settings(**changes) -> Settings:
    values = {
        "mysql_password": SecretStr("test-only-password"),
        "jwt_secret": SecretStr("test-only-jwt-secret-at-least-32-characters"),
        "tencent_plan_api_key": SecretStr("unit-test-model-key"),
        "datatap_mcp_token": SecretStr("unit-test-mcp-token"),
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_datatap_endpoints_are_derived_only_from_five_enum_values() -> None:
    config = settings()
    assert {item.value for item in DataTapService} == {
        "insight-cube-mcp",
        "social-grow-mcp",
        "social-grow-content-mcp",
        "aktools-mcp",
        "bilibili-mcp",
    }
    assert config.datatap_endpoint(DataTapService.BILIBILI).unicode_string() == (
        "https://datatap.deepminer.com.cn/api/gateway/bilibili-mcp/mcp"
    )


def test_secret_values_are_not_exposed_by_settings_repr() -> None:
    config = settings(
        tencent_plan_api_key=SecretStr("unit-test-model-key"),
        datatap_mcp_token=SecretStr("unit-test-mcp-key"),
    )
    rendered = repr(config)
    assert "unit-test-model-key" not in rendered
    assert "unit-test-mcp-key" not in rendered


@pytest.mark.parametrize(
    "changes",
    [
        {"mcp_call_points": 9},
        {"tencent_plan_model": "another-model"},
        {"tencent_plan_base_url": "https://untrusted.example/v1"},
    ],
)
def test_confirmed_provider_and_billing_constants_cannot_drift(changes) -> None:
    with pytest.raises(ValidationError):
        settings(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"tencent_plan_api_key": None},
        {"datatap_mcp_token": None},
        {
            "tencent_plan_api_key": SecretStr(""),
            "datatap_mcp_token": SecretStr("unit-test-mcp-key"),
        },
        {
            "tencent_plan_api_key": SecretStr("unit-test-model-key"),
            "datatap_mcp_token": SecretStr(""),
        },
    ],
)
def test_real_runtime_rejects_missing_credentials(changes) -> None:
    with pytest.raises(ValidationError):
        settings(**changes)


def test_real_runtime_accepts_both_credentials() -> None:
    config = settings(
        tencent_plan_api_key=SecretStr("unit-test-model-key"),
        datatap_mcp_token=SecretStr("unit-test-mcp-key"),
    )
    assert config.tencent_plan_model == "deepseek-v4-pro"
    assert config.datatap_mcp_token.get_secret_value() == "unit-test-mcp-key"
    assert config.datatap_read_timeout_seconds == 300.0


@pytest.mark.parametrize("secret_value", [" ", "\t", "\n", " \t\n"])
@pytest.mark.parametrize("secret_field", ["tencent_plan_api_key", "datatap_mcp_token"])
def test_real_runtime_rejects_whitespace_only_credentials(
    secret_field: str, secret_value: str
) -> None:
    credentials = {
        "tencent_plan_api_key": SecretStr("unit-test-model-key"),
        "datatap_mcp_token": SecretStr("unit-test-mcp-key"),
    }
    credentials[secret_field] = SecretStr(secret_value)

    with pytest.raises(ValidationError):
        settings(
            **credentials,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"model_timeout_seconds": 0},
        {"datatap_read_timeout_seconds": 0},
        {"mcp_unknown_reconcile_seconds": 0},
        {"task_lease_seconds": 0},
    ],
)
def test_runtime_durations_must_be_positive(changes) -> None:
    with pytest.raises(ValidationError):
        settings(**changes)
