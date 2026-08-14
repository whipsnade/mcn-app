"""Task 9 Pi-only 六案例隔离编排回归。"""

import importlib.util
import json
import socket
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


def _load_finalizer_module():
    script = Path(__file__).parents[2] / "scripts" / "finalize_pi_runtime_poc.py"
    spec = importlib.util.spec_from_file_location("pi_runtime_poc_finalizer", script)
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


def _finalizer_round(tmp_path: Path, *, failed: bool = False) -> tuple[object, Path, tuple[PocCase, ...]]:
    finalizer = _load_finalizer_module()
    root = tmp_path / "outputs" / "pi-runtime-poc"
    round_dir = begin_round(root, "round-finalize")
    cases = load_cases(FIXTURE)
    results = [_result(case.case_id) for case in cases]
    if failed:
        results[0] = PocCaseResult(**{**results[0].__dict__, "status": "failed"})
    write_execution_manifest(round_dir, cases, tuple(results))
    finalizer._output_root = lambda: root
    return finalizer, round_dir, cases


def test_finalizer_allows_infra_without_human_review_and_preserves_exact_manifest(tmp_path: Path) -> None:
    finalizer, round_dir, cases = _finalizer_round(tmp_path, failed=True)
    summary = finalizer.finalize_round(round_dir, FIXTURE)
    assert json.loads(summary.read_text(encoding="utf-8"))["gate"]["gate"] == "INFRA_FAILED"
    execution = json.loads((round_dir / "execution.json").read_text(encoding="utf-8"))
    assert execution["runtime"] == "pi"
    assert [item["case_id"] for item in execution["results"]] == [case.case_id for case in cases]


def test_finalizer_requires_review_rejects_secrets_and_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finalizer, round_dir, _ = _finalizer_round(tmp_path)
    with pytest.raises(ValueError, match="poc_human_review_required"):
        finalizer.finalize_round(round_dir, FIXTURE)
    (round_dir / "leak.json").write_text('{"value":"sk-test"}', encoding="utf-8")
    with pytest.raises(ValueError, match="poc_output_contains_secret"):
        finalizer.finalize_round(round_dir, FIXTURE)
    (round_dir / "leak.json").unlink()
    (round_dir / "summary.json").write_text("original", encoding="utf-8")
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network"))
    with pytest.raises(FileExistsError):
        finalizer.finalize_round(round_dir, FIXTURE)
    assert (round_dir / "summary.json").read_text(encoding="utf-8") == "original"
