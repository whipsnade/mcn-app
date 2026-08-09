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
        assert result["artifacts"][0]["exported"] is True
        assert result["artifacts"][0]["export_digest"]


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


@pytest.mark.parametrize(
    ("filename", "mutate"),
    [
        (
            "evidence.json",
            lambda payload: payload["brand-research-v1"][0].__setitem__(
                "evidence_id", "does-not-exist"
            ),
        ),
        (
            "evidence.json",
            lambda payload: payload["brand-research-v1"][0].__setitem__(
                "source_path", "/data/overview/missing"
            ),
        ),
        (
            "events.json",
            lambda payload: payload["brand-research-v1"][3].__setitem__("value", 999),
        ),
        (
            "events.json",
            lambda payload: (
                payload["brand-research-v1"][2].__setitem__("value", 999),
                payload["brand-research-v1"][3].__setitem__("value", 999),
            ),
        ),
        (
            "events.json",
            lambda payload: payload["brand-research-v1"][5].__setitem__("format", "xlsx"),
        ),
    ],
)
def test_replay_rejects_structured_evidence_tampering(
    tmp_path: Path, filename: str, mutate
) -> None:
    import json

    for path in FIXTURES.iterdir():
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    payload = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    mutate(payload)
    (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="offline_replay_"):
        run_offline_marketing_replay(tmp_path)


def test_generated_results_fail_closed_for_lineage_scope_and_candidate_tampering() -> None:
    execution = run_offline_marketing_replay(FIXTURES)
    cases = _load_cases()

    brand = deepcopy(execution.results[0])
    brand["artifacts"][0]["structured_claims"][0]["supporting_paths"] = [
        "/data/overview/missing"
    ]
    assert not evaluate_case(brand, cases[0])["numeric_lineage_complete"]

    brand = deepcopy(execution.results[0])
    brand["artifact_versions"] = ["version-tampered"]
    assert not evaluate_case(brand, cases[0])["numeric_lineage_complete"]

    brand = deepcopy(execution.results[0])
    brand["scope"]["brand"] = "另一品牌"
    assert not evaluate_case(brand, cases[0])["scope_preserved"]

    brand = deepcopy(execution.results[0])
    brand["artifacts"][0]["narrative_claims"][0]["value"] = 999
    assert not evaluate_case(brand, cases[0])["narrative_grounded"]

    kol = deepcopy(execution.results[2])
    kol["candidates"][0]["nickname"] = ""
    assert not evaluate_case(kol, cases[2])["valid_candidates"]

    partial = deepcopy(execution.results[0])
    field = partial["artifacts"][0]["canonical_data"]["data/overview/total_volume"]
    field["availability"] = "partial"
    partial["artifacts"][0]["availability"]["data/overview/total_volume"] = "partial"
    partial["artifacts"][0]["limitations"] = []
    assert not evaluate_case(partial, cases[0])["partial_limitations_complete"]


def test_replay_rejects_self_consistent_evidence_and_event_value_tampering(tmp_path: Path) -> None:
    import json

    for path in FIXTURES.iterdir():
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    evidence["brand-research-v1"][0]["value"] = 999
    (tmp_path / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    events = json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    events["brand-research-v1"][2]["value"] = 999
    events["brand-research-v1"][3]["value"] = 999
    (tmp_path / "events.json").write_text(json.dumps(events), encoding="utf-8")

    with pytest.raises(ValueError, match="offline_replay_"):
        run_offline_marketing_replay(tmp_path)
