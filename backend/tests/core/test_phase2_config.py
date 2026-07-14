import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.mcp_gateway.contracts import DataTapService


def settings(**changes) -> Settings:
    values = {
        "mysql_password": SecretStr("test-only-password"),
        "jwt_secret": SecretStr("test-only-jwt-secret-at-least-32-characters"),
        "model_provider": "fake",
        "mcp_provider": "fake",
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
        {"mcp_max_calls_per_task": 11},
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
        {
            "model_provider": "fake",
            "mcp_provider": "datatap",
            "tencent_plan_api_key": SecretStr("unit-test-model-key"),
            "datatap_mcp_token": SecretStr("unit-test-mcp-key"),
        },
        {
            "model_provider": "tencent_plan",
            "mcp_provider": "fake",
            "tencent_plan_api_key": SecretStr("unit-test-model-key"),
            "datatap_mcp_token": SecretStr("unit-test-mcp-key"),
        },
        {
            "model_provider": "tencent_plan",
            "mcp_provider": "datatap",
            "datatap_mcp_token": SecretStr("unit-test-mcp-key"),
        },
        {
            "model_provider": "tencent_plan",
            "mcp_provider": "datatap",
            "tencent_plan_api_key": SecretStr("unit-test-model-key"),
        },
        {
            "model_provider": "tencent_plan",
            "mcp_provider": "datatap",
            "tencent_plan_api_key": SecretStr(""),
            "datatap_mcp_token": SecretStr("unit-test-mcp-key"),
        },
        {
            "model_provider": "tencent_plan",
            "mcp_provider": "datatap",
            "tencent_plan_api_key": SecretStr("unit-test-model-key"),
            "datatap_mcp_token": SecretStr(""),
        },
    ],
)
def test_production_rejects_fake_providers_or_missing_credentials(changes) -> None:
    with pytest.raises(ValidationError):
        settings(app_env="production", auth_mode="jwt", **changes)


def test_production_accepts_confirmed_providers_with_both_credentials() -> None:
    config = settings(
        app_env="production",
        auth_mode="jwt",
        model_provider="tencent_plan",
        mcp_provider="datatap",
        tencent_plan_api_key=SecretStr("unit-test-model-key"),
        datatap_mcp_token=SecretStr("unit-test-mcp-key"),
    )
    assert config.model_provider == "tencent_plan"
    assert config.mcp_provider == "datatap"


@pytest.mark.parametrize(
    "changes",
    [
        {"model_timeout_seconds": 0},
        {"mcp_unknown_reconcile_seconds": 0},
        {"task_lease_seconds": 0},
    ],
)
def test_runtime_durations_must_be_positive(changes) -> None:
    with pytest.raises(ValidationError):
        settings(**changes)
