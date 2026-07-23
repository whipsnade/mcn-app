from types import SimpleNamespace

import pytest

from app.goals.planner import GoalPlannerService
from app.tasks import dependencies


@pytest.mark.parametrize(
    ("enabled", "expected_type"),
    [(False, type(None)), (True, GoalPlannerService)],
)
def test_task_runtime_injects_shadow_only_when_enabled(
    monkeypatch,
    enabled: bool,
    expected_type: type,
) -> None:
    settings = SimpleNamespace(
        goal_planner_shadow_enabled=enabled,
        task_lease_seconds=60,
        mcp_unknown_reconcile_seconds=300,
    )
    fake_model = object()
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(dependencies, "get_model_adapter", lambda: fake_model)
    monkeypatch.setattr(dependencies, "get_mcp_transport", lambda: object())

    runtime = dependencies.TaskExecutionDependencies()
    executor = runtime.create_executor()

    assert isinstance(executor.goal_planner_shadow, expected_type)
