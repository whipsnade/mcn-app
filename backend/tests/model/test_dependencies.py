from types import SimpleNamespace

from pydantic import SecretStr

from app.core.config import Settings
from app.model import dependencies
from app.model.fake import FakeModelAdapter
from app.model.tencent_plan import TencentPlanAdapter


def settings(**changes) -> Settings:
    values = {
        "mysql_password": SecretStr("test-only-password"),
        "jwt_secret": SecretStr("test-only-jwt-secret-at-least-32-characters"),
        "model_provider": "fake",
        "mcp_provider": "fake",
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_process_dependency_defaults_to_network_free_fake(monkeypatch) -> None:
    dependencies.get_model_adapter.cache_clear()
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings())

    adapter = dependencies.get_model_adapter()

    assert isinstance(adapter, FakeModelAdapter)
    dependencies.get_model_adapter.cache_clear()


def test_tencent_client_disables_sdk_retries_and_uses_fixed_settings(monkeypatch) -> None:
    captured = {}
    completions = SimpleNamespace(create=None)

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=completions), close=None)

    monkeypatch.setattr("app.model.tencent_plan.AsyncOpenAI", fake_openai)
    config = settings(
        model_provider="tencent_plan",
        tencent_plan_api_key=SecretStr("unit-test-key"),
    )

    adapter = TencentPlanAdapter.from_settings(config)

    assert captured == {
        "api_key": "unit-test-key",
        "base_url": "https://tokenhub.tencentmaas.com/plan/v3",
        "max_retries": 0,
        "timeout": 60.0,
    }
    assert adapter.model == "deepseek-v4-pro"
