from pydantic import SecretStr

from app.core.config import Settings
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway import service as gateway_service


def settings(**changes: object) -> Settings:
    values = {
        "mysql_password": SecretStr("test-only-password"),
        "jwt_secret": SecretStr("test-only-jwt-secret-at-least-32-characters"),
        "tencent_plan_api_key": SecretStr("unit-test-model-key"),
        "datatap_mcp_token": SecretStr("unit-test-mcp-token"),
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_process_dependency_always_builds_datatap_transport(monkeypatch) -> None:
    gateway_service.get_mcp_transport.cache_clear()
    monkeypatch.setattr(gateway_service, "get_settings", lambda: settings())

    transport = gateway_service.get_mcp_transport()
    assert isinstance(transport, DataTapTransport)
    assert transport._read_timeout_seconds == 60.0

    gateway_service.get_mcp_transport.cache_clear()
