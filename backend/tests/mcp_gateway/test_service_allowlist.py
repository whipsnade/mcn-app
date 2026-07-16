import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.mcp_gateway.contracts import DataTapService, McpCallStatus


def settings() -> Settings:
    return Settings(
        _env_file=None,
        mysql_password=SecretStr("test-only-password"),
        jwt_secret=SecretStr("test-only-jwt-secret-at-least-32-characters"),
        tencent_plan_api_key=SecretStr("unit-test-model-key"),
        datatap_mcp_token=SecretStr("unit-test-mcp-token"),
    )


def test_mcp_call_status_values_are_frozen() -> None:
    assert {item.value for item in McpCallStatus} == {
        "planned",
        "reserved",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "settled",
        "released",
    }


def test_datatap_endpoint_rejects_an_arbitrary_service_string() -> None:
    with pytest.raises(TypeError):
        settings().datatap_endpoint("not-allowlisted")  # type: ignore[arg-type]


@pytest.mark.parametrize("service", list(DataTapService))
def test_every_allowlisted_service_maps_to_its_gateway(service: DataTapService) -> None:
    assert settings().datatap_endpoint(service).unicode_string() == (
        f"https://datatap.deepminer.com.cn/api/gateway/{service.value}/mcp"
    )
