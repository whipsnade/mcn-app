"""Task 9 Pi-only 六案例隔离编排回归。"""

import importlib.util
import sys
from pathlib import Path

import pytest

from app.pi_runtime_poc.comparison import (
    PocCase,
    PocCaseResult,
    begin_round,
    load_cases,
    write_execution_manifest,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "pi_runtime_poc" / "cases.json"


def _load_runner_module():
    script = Path(__file__).parents[2] / "scripts" / "run_pi_runtime_poc.py"
    spec = importlib.util.spec_from_file_location("pi_runtime_poc_task9_runner", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(case_id: str, *, artifacts: tuple[str, ...] = ()) -> PocCaseResult:
    return PocCaseResult(
        case_id=case_id,
        runtime="pi",
        run_id=f"run-{case_id}",
        status="completed",
        error_code=None,
        outcome="completed",
        artifact_versions=artifacts,
        evidence_ids=(),
        metrics={"datatap_tool_calls": 0, "points_reserved": 0, "points_settled": 0},
        diagnostic_path=f"/tmp/{case_id}/pi.json",
        hard_checks={},
    )


def test_parse_args_accepts_only_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module()
    monkeypatch.setattr(sys, "argv", ["run_pi_runtime_poc.py", "--case", "all", "--runtime", "pi"])

    assert runner.parse_args().runtime == "pi"


@pytest.mark.parametrize("runtime", ["current", "both"])
def test_parse_args_rejects_current_before_preflight(
    monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    runner = _load_runner_module()
    monkeypatch.setattr(sys, "argv", ["run_pi_runtime_poc.py", "--runtime", runtime])

    with pytest.raises(SystemExit):
        runner.parse_args()


def test_execution_manifest_requires_all_six_pi_results(tmp_path: Path) -> None:
    cases = load_cases(FIXTURE)
    round_dir = begin_round(tmp_path, "round-1")

    with pytest.raises(ValueError, match="poc_execution_requires_exact_cases"):
        write_execution_manifest(round_dir, cases, (_result(cases[0].case_id),))


async def test_brand_failure_only_skips_drilldown_and_keeps_later_cases_running(tmp_path: Path) -> None:
    runner = _load_runner_module()
    cases = load_cases(FIXTURE)
    round_dir = begin_round(tmp_path, "round-2")
    executed: list[str] = []

    class FakeExecutor:
        async def execute(self, case: PocCase, *, prior_run_id: str | None = None) -> PocCaseResult:
            executed.append(case.case_id)
            if case.case_id == "brand-research-v1":
                raise RuntimeError("fake brand failure")
            return _result(case.case_id, artifacts=("artifact-v1",) if case.required_artifact_type else ())

    results = await runner.run_selected_cases(cases, FakeExecutor(), round_dir)

    assert [result.case_id for result in results] == [case.case_id for case in cases]
    assert results[0].status == "failed"
    assert results[3].status == "skipped_dependency"
    assert results[3].error_code == "poc_dependency_artifact_unavailable"
    assert executed == [
        "brand-research-v1",
        "campaign-evaluation-v1",
        "kol-selection-v1",
        "scope-clarification-v1",
        "non-marketing-v1",
    ]
