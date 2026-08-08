"""Task 7：Marketing Capability Pack B0 的纯本地六案例回放。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from app.pi_runtime_poc.gate import HARD_CHECKS, evaluate_case
from app.pi_runtime_poc.replay import run_offline_marketing_replay

FIXTURES = Path(__file__).parents[2] / "fixtures" / "pi_runtime_poc" / "marketing_b0"
CASE_FIXTURE = FIXTURES / "cases.json"


def _load_cases() -> list[dict[str, object]]:
    import json

    return json.loads(CASE_FIXTURE.read_text(encoding="utf-8"))


def test_offline_replay_returns_six_deterministic_pi_cases_without_services() -> None:
    first = run_offline_marketing_replay(FIXTURES)
    second = run_offline_marketing_replay(FIXTURES)

    assert first.as_dict() == second.as_dict()
    assert first.runtime == "pi"
    assert [item["case_id"] for item in first.results] == [
        "brand-research-v1",
        "campaign-evaluation-v1",
        "kol-selection-v1",
        "artifact-drilldown-v1",
        "scope-clarification-v1",
        "non-marketing-v1",
    ]
    assert all(item["metrics"]["datatap_tool_calls"] == 0 for item in first.results[3:])
    assert first.fixture_digest
    assert first.summary == second.summary
    assert first.summary["hard_check_gate"] == "PASS"
    assert first.summary["human_review"] == "not_provided"


def test_three_report_cases_pass_all_hard_checks_without_fake_human_scores() -> None:
    execution = run_offline_marketing_replay(FIXTURES)
    cases = _load_cases()
    for result, fixture in zip(execution.results[:3], cases[:3], strict=True):
        checks = evaluate_case(result, fixture)
        assert tuple(checks) == HARD_CHECKS
        assert all(checks.values()), (result["case_id"], checks)
        assert result["artifact_versions"]
        assert result["evidence_ids"]


def test_drilldown_is_bound_to_exact_published_version_and_never_calls_datatap() -> None:
    execution = run_offline_marketing_replay(FIXTURES)
    cases = _load_cases()
    result = execution.results[3]
    checks = evaluate_case(result, cases[3])

    assert checks["drilldown_bound_to_version"]
    assert checks["drilldown_grounded"]
    assert result["metrics"]["datatap_tool_calls"] == 0
    assert result["artifact_versions"] == []

    tampered = deepcopy(result)
    tampered["drilldown_version_id"] = "version-not-published"
    assert not evaluate_case(tampered, cases[3])["drilldown_bound_to_version"]


@pytest.mark.parametrize(
    ("index", "check"),
    ((4, "clarification_no_tool_call"), (5, "non_marketing_refused")),
)
def test_clarification_and_refusal_have_zero_artifacts_and_zero_datatap(
    index: int, check: str
) -> None:
    execution = run_offline_marketing_replay(FIXTURES)
    cases = _load_cases()
    result = execution.results[index]

    assert evaluate_case(result, cases[index])[check]
    assert result["artifact_versions"] == []
    assert result["metrics"]["datatap_tool_calls"] == 0


def test_replay_rejects_tampered_case_order_or_event_manifest(tmp_path: Path) -> None:
    import json

    for path in FIXTURES.iterdir():
        path_target = tmp_path / path.name
        path_target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    cases = json.loads((tmp_path / "cases.json").read_text(encoding="utf-8"))
    cases[0], cases[1] = cases[1], cases[0]
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(ValueError, match="offline_replay_case_order_invalid"):
        run_offline_marketing_replay(tmp_path)
