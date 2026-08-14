"""Task 8D：POC 诊断只保留可安全归因的数据库错误元数据。"""

from pathlib import Path

from sqlalchemy.exc import OperationalError

from app.pi_runtime_poc.diagnostics import safe_db_diagnostic


class _MysqlDeadlock(Exception):
    errno = 1213

    def __str__(self) -> str:
        return "Deadlock found for key 'uq_agent_steps_run_sequence'; Bearer should-not-leak"


def test_safe_db_diagnostic_keeps_only_error_type_errno_and_constraint() -> None:
    error = OperationalError(
        "INSERT INTO agent_steps VALUES (:token)",
        {"token": "sk-should-not-leak"},
        _MysqlDeadlock(),
    )

    diagnostic = safe_db_diagnostic(error)

    assert diagnostic == {
        "exception_type": "OperationalError",
        "mysql_errno": 1213,
        "constraint": "uq_agent_steps_run_sequence",
    }
    assert "sk-" not in str(diagnostic)
    assert "Bearer" not in str(diagnostic)
    assert "INSERT" not in str(diagnostic)


def test_safe_db_diagnostic_does_not_copy_unknown_exception_text() -> None:
    diagnostic = safe_db_diagnostic(RuntimeError("Bearer secret-value sk-should-not-leak"))

    assert diagnostic == {"exception_type": "RuntimeError"}


def test_real_poc_launcher_keeps_safe_diagnostics_separate_from_uvicorn_stderr() -> None:
    launcher = Path(__file__).parents[2] / "scripts" / "run_pi_runtime_poc.sh"
    content = launcher.read_text(encoding="utf-8")

    assert "PI_RUNTIME_POC_DIAGNOSTIC_LOG" in content
    assert '>>"${PI_RUNTIME_POC_DIAGNOSTIC_LOG}" 2>&1 &' not in content
    assert ">/dev/null 2>/dev/null &" in content


def test_real_poc_launcher_allows_a_cold_minimal_service_to_become_healthy() -> None:
    """冷启动超过旧的 30 秒窗口时，不得在真实 round 创建前过早退出。"""
    launcher = Path(__file__).parents[2] / "scripts" / "run_pi_runtime_poc.sh"
    content = launcher.read_text(encoding="utf-8")

    assert "$(seq 1 60)" in content
