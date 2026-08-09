import pytest

from app.tenancy.service import effective_runtime_backend


@pytest.mark.parametrize(
    ("tenant_backend", "kill_switch", "expected"),
    [("current", False, "current"), ("pi", False, "pi"), ("current", True, "current"), ("pi", True, "current")],
)
def test_kill_switch_only_changes_new_run_backend(tenant_backend: str, kill_switch: bool, expected: str) -> None:
    assert effective_runtime_backend(tenant_backend, kill_switch=kill_switch) == expected


def test_unknown_backend_fails_closed() -> None:
    with pytest.raises(ValueError, match="runtime_backend_invalid"):
        effective_runtime_backend("shadow", kill_switch=False)
