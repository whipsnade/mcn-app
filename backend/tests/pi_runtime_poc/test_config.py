import secrets

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.pi_runtime_poc.auth import PiPocSettingsGuard


@pytest.fixture
def settings_factory():
    def build(**changes: object) -> Settings:
        values: dict[str, object] = {
            "app_env": "test",
            "mysql_database": "kol_insight_pi_poc",
            "mysql_password": SecretStr(secrets.token_urlsafe(24)),
            "jwt_secret": SecretStr(secrets.token_urlsafe(32)),
            "tencent_plan_api_key": SecretStr(secrets.token_urlsafe(24)),
            "datatap_mcp_token": SecretStr(secrets.token_urlsafe(24)),
            "pi_runtime_poc_enabled": True,
            "pi_runtime_poc_internal_secret": SecretStr(secrets.token_urlsafe(32)),
        }
        values.update(changes)
        return Settings(_env_file=None, **values)

    return build


def test_poc_guard_rejects_non_poc_database(settings_factory) -> None:
    settings = settings_factory(mysql_database="kol_insight")

    with pytest.raises(RuntimeError, match="pi_poc_database_required"):
        PiPocSettingsGuard.assert_safe(settings)


def test_poc_guard_accepts_exact_database(settings_factory) -> None:
    settings = settings_factory()

    PiPocSettingsGuard.assert_safe(settings)
