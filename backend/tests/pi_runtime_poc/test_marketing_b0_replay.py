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
            lambda payload: payload["brand-research-v1"][5].__setitem__("format", "json"),
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

    kol = deepcopy(execution.results[2])
    kol["artifacts"][0]["payload"]["data"]["items"][0]["score_snapshot"][
        "value_score"
    ] = 999
    assert not evaluate_case(kol, cases[2])["valid_candidates"]

    kol = deepcopy(execution.results[2])
    snapshot = kol["artifacts"][0]["payload"]["data"]["items"][0]["score_snapshot"]
    snapshot["dimensions"] = {"followers": snapshot["dimensions"]["followers"]}
    snapshot["effect_score"] = snapshot["dimensions"]["followers"]["weighted_score"]
    snapshot["value_score"] = snapshot["effect_score"] + snapshot["price_efficiency_score"]
    assert not evaluate_case(kol, cases[2])["valid_candidates"]

    kol = deepcopy(execution.results[2])
    snapshot = kol["artifacts"][0]["payload"]["data"]["items"][0]["score_snapshot"]
    snapshot["dimensions"]["followers"]["weight"] = 999
    snapshot["dimensions"]["followers"]["weighted_score"] = 499.5
    snapshot["effect_score"] = sum(
        dimension["weighted_score"] for dimension in snapshot["dimensions"].values()
    )
    snapshot["value_score"] = snapshot["effect_score"] + snapshot["price_efficiency_score"]
    assert not evaluate_case(kol, cases[2])["valid_candidates"]

    kol = deepcopy(execution.results[2])
    kol["artifacts"][0]["lineage_source_paths"]["/data/summary/selected_count"] = [
        "/evil"
    ]
    assert not evaluate_case(kol, cases[2])["valid_candidates"]

    kol = deepcopy(execution.results[2])
    kol["artifacts"][0]["lineage_source_paths"][
        "/data/items/0/score_snapshot/value_score"
    ] = ["/evil"]
    assert not evaluate_case(kol, cases[2])["valid_candidates"]

    kol = deepcopy(execution.results[2])
    scoring = kol["artifacts"][0]["payload"]["data"]["scoring"]
    scoring["weights"] = {"foo": 70}
    snapshot = kol["artifacts"][0]["payload"]["data"]["items"][0]["score_snapshot"]
    follower_dimension = snapshot["dimensions"]["followers"]
    snapshot["dimensions"] = {
        "foo": {**follower_dimension, "weight": 70, "weighted_score": 35}
    }
    snapshot["effect_score"] = 35
    snapshot["value_score"] = snapshot["effect_score"] + snapshot["price_efficiency_score"]
    lineage_paths = kol["artifacts"][0]["lineage_source_paths"]
    for path in list(lineage_paths):
        if "/dimensions/" in path:
            del lineage_paths[path]
    lineage_paths.update(
        {
            "/data/items/0/score_snapshot/dimensions/foo/raw_score": ["/0/platform"],
            "/data/items/0/score_snapshot/dimensions/foo/weighted_score": ["/0/platform"],
        }
    )
    assert not evaluate_case(kol, cases[2])["valid_candidates"]

    kol = deepcopy(execution.results[2])
    kol["artifacts"][0]["limitations"] = [
        {"affected_paths": ["/data/summary/selected_count"]}
    ]
    assert not evaluate_case(kol, cases[2])["partial_limitations_complete"]

    partial = deepcopy(execution.results[0])
    field = next(
        item
        for item in partial["artifacts"][0]["canonical_data"]
        if item["path"] == "/data/overview/total_volume"
    )
    field["availability"] = "partial"
    partial["artifacts"][0]["availability"]["data/overview/total_volume"] = "partial"
    partial["artifacts"][0]["limitations"] = []
    assert not evaluate_case(partial, cases[0])["partial_limitations_complete"]


@pytest.mark.parametrize("availability", [None, {}, {"items": "partial"}])
def test_kol_gate_rejects_missing_or_malformed_availability(
    availability: dict[str, object] | None,
) -> None:
    execution = run_offline_marketing_replay(FIXTURES)
    cases = _load_cases()
    kol = deepcopy(execution.results[2])
    payload = kol["artifacts"][0]["payload"]
    if availability is None:
        payload.pop("availability", None)
    else:
        payload["availability"] = availability

    checks = evaluate_case(kol, cases[2])
    assert checks["numeric_lineage_complete"] is False
    assert checks["narrative_grounded"] is False
    assert checks["partial_limitations_complete"] is False


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


@pytest.mark.parametrize(
    ("filename", "mutate"),
    [
        (
            "evidence.json",
            lambda payload: payload["brand-research-v1"][0].__setitem__("unit", "CNY"),
        ),
        (
            "events.json",
            lambda payload: payload["brand-research-v1"][-1].__setitem__("outcome", "failed"),
        ),
        (
            "events.json",
            lambda payload: payload["scope-clarification-v1"][1].__setitem__("outcome", "refused"),
        ),
        (
            "events.json",
            lambda payload: payload["non-marketing-v1"][1].__setitem__("outcome", "clarification_requested"),
        ),
        (
            "events.json",
            lambda payload: payload["brand-research-v1"][4].update(
                {"version_id": "other-version", "artifact_type": "campaign_report_v2"}
            ),
        ),
        (
            "events.json",
            lambda payload: payload["brand-research-v1"][5].update(
                {"version_id": "other-version", "artifact_type": "campaign_report_v2"}
            ),
        ),
    ],
)
def test_replay_rejects_event_and_evidence_semantic_tampering(
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


def test_replay_calls_shared_capability_pack_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.marketing_capability_pack import runtime

    def fail(*args, **kwargs):
        raise RuntimeError("shared_capability_entry_called")

    monkeypatch.setattr(runtime, "build_marketing_run_capability", fail)
    with pytest.raises(RuntimeError, match="shared_capability_entry_called"):
        run_offline_marketing_replay(FIXTURES)


@pytest.mark.parametrize(
    ("module_name", "attribute", "error_code"),
    (
        (
            "app.agent_artifacts.builders.brand",
            "build_brand_report_draft",
            "shared_brand_builder_called",
        ),
        (
            "app.agent_artifacts.builders.campaign",
            "build_campaign_report_draft",
            "shared_campaign_builder_called",
        ),
        (
            "app.agent_artifacts.builders.kol_selection",
            "build_kol_selection_draft",
            "shared_kol_builder_called",
        ),
        (
            "app.agent_artifacts.validation",
            "ArtifactPayloadValidator.validate_revision_payload_collecting",
            "shared_validator_called",
        ),
        (
            "app.agent_artifacts.publication_core",
            "validate_payload_for_publication",
            "shared_publication_called",
        ),
        (
            "app.agent_artifacts.exporters",
            "export_artifact",
            "shared_exporter_called",
        ),
    ),
)
def test_replay_calls_shared_production_chain(
    monkeypatch: pytest.MonkeyPatch, module_name: str, attribute: str, error_code: str
) -> None:
    import importlib

    module = importlib.import_module(module_name)
    target = module
    parts = attribute.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)

    def fail(*args, **kwargs):
        raise RuntimeError(error_code)

    monkeypatch.setattr(target, parts[-1], fail)
    with pytest.raises(RuntimeError, match=error_code):
        run_offline_marketing_replay(FIXTURES)


def test_replay_produces_complete_v3_kol_payload() -> None:
    execution = run_offline_marketing_replay(FIXTURES)
    payload = execution.results[2]["artifacts"][0]["payload"]
    scope = payload["scope"]
    assert {
        "brand", "category", "platforms", "audience", "region", "age_range", "period",
        "budget", "filters", "ranking_mode", "top_limit", "scoring_version",
    } <= set(scope)
    assert payload["schema_version"] == "kol_selection_v3"
    assert payload["data"]["items"]
    assert payload["data"]["summary"]["selected_count"] == len(payload["data"]["items"])


def test_replay_export_digest_is_digest_of_exported_bytes() -> None:
    import hashlib
    from types import SimpleNamespace

    from app.agent_artifacts.exporters import export_artifact
    from app.agent_artifacts.offline_pipeline import _canonical_export_bytes

    execution = run_offline_marketing_replay(FIXTURES)
    artifact = execution.results[0]["artifacts"][0]
    exported = export_artifact(
        SimpleNamespace(
            schema_version=artifact["artifact_type"],
            payload_json=artifact["payload"],
            status="published",
            validation_json={"valid": True},
        )
    )
    assert artifact["export_digest"] == hashlib.sha256(_canonical_export_bytes(exported)).hexdigest()
