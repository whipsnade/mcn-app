"""Task 6：纯值对象 B0 hard-check Gate。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.pi_runtime_poc.gate import HARD_CHECKS, evaluate_case, finalize_execution


def _report_fixture() -> dict:
    return {
        "case_id": "brand-research-v1",
        "expected_behavior": "report",
        "required_artifact_type": "brand_report_v3",
        "published_version_id": "version-brand",
        "scope": {"brand": "某品牌", "platforms": ["xiaohongshu"]},
        "evidence_manifest": [
            {
                "evidence_id": "evidence-brand",
                "version_id": "version-brand",
                "run_id": "run-brand",
                "session_id": "session-brand",
                "source_path": "/data/overview/total_volume",
                "value": 10,
            }
        ],
    }


def _report_result() -> dict:
    return {
        "case_id": "brand-research-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "run_id": "run-brand",
        "session_id": "session-brand",
        "artifact_versions": ["version-brand"],
        "evidence_ids": ["evidence-brand"],
        "scope": {"brand": "某品牌", "platforms": ["xiaohongshu"]},
        "artifacts": [
            {
                "version_id": "version-brand",
                "artifact_type": "brand_report_v3",
                "numeric_lineage_complete": True,
                "narrative_grounded": True,
                "partial_limitations_complete": True,
                "status": "published",
                "validation_json": {"valid": True},
                "payload": {"data": {"overview": {"total_volume": 10}}},
                "canonical_data": {
                    "data/overview/total_volume": {
                        "path": "/data/overview/total_volume",
                        "value": 10,
                        "availability": "complete",
                        "evidence_ids": ["evidence-brand"],
                        "unit": "mentions",
                    }
                },
                "field_lineage": {
                    "data/overview/total_volume": {"evidence_ids": ["evidence-brand"]}
                },
                "structured_claims": [
                    {
                        "path": "/data/overview/total_volume",
                        "value": 10,
                        "supporting_paths": ["data/overview/total_volume"],
                        "evidence_ids": ["evidence-brand"],
                    }
                ],
                "narrative_claims": [
                    {
                        "path": "/data/overview/total_volume",
                        "value": 10,
                        "supporting_paths": ["data/overview/total_volume"],
                        "evidence_ids": ["evidence-brand"],
                    }
                ],
                "availability": {"data/overview/total_volume": "complete"},
                "limitations": [],
                "evidence_manifest": [
                    {
                        "evidence_id": "evidence-brand",
                        "version_id": "version-brand",
                        "run_id": "run-brand",
                        "session_id": "session-brand",
                        "source_path": "/data/overview/total_volume",
                        "value": 10,
                    }
                ],
            }
        ],
        "metrics": {"datatap_tool_calls": 1},
        "candidates": [],
    }


def test_evaluate_case_returns_all_ten_structured_checks() -> None:
    checks = evaluate_case(_report_result(), _report_fixture())
    assert tuple(checks) == HARD_CHECKS
    assert all(checks.values())


@pytest.mark.parametrize("field", HARD_CHECKS)
def test_each_hard_check_can_fail_closed(field: str) -> None:
    fixture = _report_fixture()
    result = _report_result()
    if field == "scope_preserved":
        result["scope"] = {"brand": "other"}
    elif field == "numeric_lineage_complete":
        result["evidence_ids"] = ["does-not-exist"]
    elif field == "narrative_grounded":
        result["artifacts"][0]["narrative_claims"][0]["value"] = 99
    elif field == "partial_limitations_complete":
        result["artifacts"][0]["canonical_data"]["data/overview/total_volume"][
            "availability"
        ] = "partial"
    elif field == "no_duplicate_report":
        result["artifact_versions"] = ["version-brand", "version-brand"]
    else:
        # These checks are behavior-specific; a non-applicable report must not
        # turn a failed explicit signal into a pass.
        result[field] = False
    assert evaluate_case(result, fixture)[field] is False


def test_kol_candidates_need_identity_nickname_platform_and_observed_score() -> None:
    fixture = {
        "case_id": "kol-selection-v1",
        "expected_behavior": "report",
        "required_artifact_type": "kol_selection_v3",
        "scope": {"platforms": ["xiaohongshu"]},
    }
    result = {
        "case_id": "kol-selection-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "artifact_versions": ["version-kol"],
        "artifacts": [{"artifact_type": "kol_selection_v3", "valid_candidates": False}],
        "candidates": [{"nickname": "", "platform": "unknown", "kol_uid": "", "score_inputs": {}}],
        "metrics": {"datatap_tool_calls": 1},
    }
    assert evaluate_case(result, fixture)["valid_candidates"] is False


def test_drilldown_must_bind_exact_version_without_datatap() -> None:
    fixture = {
        "case_id": "artifact-drilldown-v1",
        "expected_behavior": "drilldown",
        "depends_on_case_id": "brand-research-v1",
        "published_version_id": "version-brand",
    }
    result = {
        "case_id": "artifact-drilldown-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "artifact_versions": [],
        "drilldown_version_id": "version-brand",
        "drilldown_grounded": True,
        "metrics": {"datatap_tool_calls": 0},
    }
    checks = evaluate_case(result, fixture)
    assert checks["drilldown_bound_to_version"]
    assert not checks["drilldown_grounded"]

    bad = deepcopy(result)
    bad["drilldown_version_id"] = "other-version"
    assert not evaluate_case(bad, fixture)["drilldown_bound_to_version"]


def test_clarification_and_refusal_require_zero_artifacts_and_datatap() -> None:
    clarify_fixture = {
        "case_id": "scope-clarification-v1",
        "expected_behavior": "clarify",
        "required_artifact_type": None,
    }
    clarify_result = {
        "case_id": "scope-clarification-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "clarification_requested",
        "artifact_versions": [],
        "metrics": {"datatap_tool_calls": 0},
    }
    assert evaluate_case(clarify_result, clarify_fixture)["clarification_no_tool_call"]

    refuse_fixture = {
        "case_id": "non-marketing-v1",
        "expected_behavior": "refuse",
        "required_artifact_type": None,
    }
    refuse_result = {
        "case_id": "non-marketing-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "refused",
        "artifact_versions": [],
        "metrics": {"datatap_tool_calls": 0},
    }
    assert evaluate_case(refuse_result, refuse_fixture)["non_marketing_refused"]


def _review() -> dict:
    return {
        "reports": {
            key: {
                "factuality": {"score": 3, "reason": "evidence"},
                "insight": {"score": 3, "reason": "evidence"},
                "actionability": {"score": 3, "reason": "evidence"},
                "limitations": {"score": 3, "reason": "evidence"},
            }
            for key in (
                "brand-research-v1",
                "campaign-evaluation-v1",
                "kol-selection-v1",
            )
        }
    }


def test_finalize_execution_fails_if_any_hard_check_fails() -> None:
    fixture = [_report_fixture()]
    execution = {"runtime": "pi", "results": [_report_result()]}
    summary = finalize_execution(execution, fixture, _review())
    assert summary.gate == "PASS"
    bad = deepcopy(execution)
    bad["results"][0]["artifacts"][0]["numeric_lineage_complete"] = False
    assert finalize_execution(bad, fixture, _review()).gate == "EVALUATED_FAIL"


def test_finalize_execution_requires_review_only_after_cases_are_evaluable() -> None:
    execution = {"runtime": "pi", "results": [_report_result()]}
    with pytest.raises(ValueError, match="poc_human_review_required"):
        finalize_execution(execution, [_report_fixture()], None)


def _structured_lineage_fixture() -> tuple[dict, dict]:
    fixture = {
        "case_id": "brand-research-v1",
        "expected_behavior": "report",
        "required_artifact_type": "brand_report_v3",
        "scope": {"brand": "某品牌", "platforms": ["xiaohongshu"]},
        "evidence_manifest": [
            {
                "evidence_id": "ev-volume",
                "version_id": "version-brand",
                "run_id": "run-brand",
                "session_id": "session-brand",
                "source_path": "/data/overview/total_volume",
                "value": 10,
                "unit": "mentions",
            }
        ],
        "expected_fields": {
            "data/overview/total_volume": {
                "value": 10,
                "unit": "mentions",
                "availability": "complete",
                "evidence_ids": ["ev-volume"],
            }
        },
    }
    result = {
        "case_id": "brand-research-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "run_id": "run-brand",
        "session_id": "session-brand",
        "artifact_versions": ["version-brand"],
        "evidence_ids": ["ev-volume"],
        "scope": {"brand": "某品牌", "platforms": ["xiaohongshu"]},
        "artifacts": [
            {
                "version_id": "version-brand",
                "artifact_type": "brand_report_v3",
                "status": "published",
                "validation_json": {"valid": True},
                "numeric_lineage_complete": True,
                "narrative_grounded": True,
                "partial_limitations_complete": True,
                "payload": {"data": {"overview": {"total_volume": 10}}},
                "canonical_data": {
                    "data/overview/total_volume": {
                        "path": "/data/overview/total_volume",
                        "value": 10,
                        "availability": "complete",
                        "evidence_ids": ["ev-volume"],
                        "unit": "mentions",
                    }
                },
                "field_lineage": {
                    "data/overview/total_volume": {"evidence_ids": ["ev-volume"]}
                },
                "structured_claims": [
                    {
                        "path": "/data/overview/total_volume",
                        "value": 10,
                        "supporting_paths": ["data/overview/total_volume"],
                        "evidence_ids": ["ev-volume"],
                    }
                ],
                "narrative_claims": [
                    {
                        "path": "/data/overview/total_volume",
                        "value": 10,
                        "supporting_paths": ["data/overview/total_volume"],
                        "evidence_ids": ["ev-volume"],
                    }
                ],
                "availability": {"data/overview/total_volume": "complete"},
                "limitations": [],
                "evidence_manifest": [
                    {
                        "evidence_id": "ev-volume",
                        "version_id": "version-brand",
                        "run_id": "run-brand",
                        "session_id": "session-brand",
                        "source_path": "/data/overview/total_volume",
                        "value": 10,
                        "unit": "mentions",
                    }
                ],
            }
        ],
        "metrics": {"datatap_tool_calls": 1},
    }
    return fixture, result


def test_structured_lineage_tampering_rejects_self_reported_true_flags() -> None:
    fixture, result = _structured_lineage_fixture()
    assert all(evaluate_case(result, fixture).values())

    tampered = deepcopy(result)
    tampered["evidence_ids"] = ["does-not-exist"]
    tampered["artifacts"][0]["validation_json"] = {}
    tampered["artifacts"][0]["narrative_claims"][0]["value"] = 99
    checks = evaluate_case(tampered, fixture)

    assert checks["numeric_lineage_complete"] is False
    assert checks["narrative_grounded"] is False
    assert checks["partial_limitations_complete"] is False


def test_gate_manifest_version_and_source_path_are_trusted_from_fixture_only() -> None:
    fixture = _report_fixture()
    result = _report_result()

    no_manifest = deepcopy(fixture)
    no_manifest.pop("evidence_manifest")
    assert evaluate_case(result, no_manifest)["numeric_lineage_complete"] is False

    wrong_path = deepcopy(fixture)
    wrong_path["evidence_manifest"][0]["source_path"] = "/data/overview/other"
    assert evaluate_case(result, wrong_path)["numeric_lineage_complete"] is False

    wrong_version = deepcopy(result)
    wrong_version["artifact_versions"] = ["evil-version"]
    wrong_version["artifacts"][0]["version_id"] = "evil-version"
    assert evaluate_case(wrong_version, fixture)["numeric_lineage_complete"] is False

    duplicate_manifest = deepcopy(fixture)
    duplicate_manifest["evidence_manifest"].append(
        duplicate_manifest["evidence_manifest"][0].copy()
    )
    assert evaluate_case(result, duplicate_manifest)["numeric_lineage_complete"] is False


def test_numeric_lineage_rejects_canonical_unit_mismatch() -> None:
    fixture, result = _structured_lineage_fixture()
    result["artifacts"][0]["canonical_data"]["data/overview/total_volume"]["unit"] = "CNY"
    assert evaluate_case(result, fixture)["numeric_lineage_complete"] is False


def test_no_duplicate_report_checks_raw_artifacts_even_when_version_declared_once() -> None:
    fixture, result = _structured_lineage_fixture()
    result["artifacts"].append(deepcopy(result["artifacts"][0]))
    assert evaluate_case(result, fixture)["no_duplicate_report"] is False


def test_kol_candidates_are_read_from_published_payload_and_must_match_scope() -> None:
    fixture = {
        "case_id": "kol-selection-v1",
        "expected_behavior": "report",
        "required_artifact_type": "kol_selection_v3",
        "scope": {"platforms": ["douyin", "xiaohongshu"]},
    }
    candidate = {
        "nickname": "越界达人",
        "platform": "bilibili",
        "kol_uid": "kol-outside",
        "score_snapshot": {
            "dimensions": {"engagement": {"source": "evidence"}}
        },
    }
    result = {
        "case_id": "kol-selection-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "scope": fixture["scope"],
        "artifact_versions": ["version-kol"],
        "evidence_ids": [],
        "artifacts": [
            {
                "version_id": "version-kol",
                "artifact_type": "kol_selection_v3",
                "status": "published",
                "payload": {"data": {"items": [candidate], "summary": {"selected_count": 1}}},
            }
        ],
        # 不得信任这个与已发布 payload 不一致的副本。
        "candidates": [
            {"nickname": "合法副本", "platform": "douyin", "kol_uid": "kol-in-scope", "score_inputs": {"x": 1}}
        ],
        "metrics": {"datatap_tool_calls": 0},
    }
    assert evaluate_case(result, fixture)["valid_candidates"] is False


def test_kol_hard_checks_reject_unpublished_self_reported_payload() -> None:
    fixture = {
        "case_id": "kol-selection-v1",
        "expected_behavior": "report",
        "required_artifact_type": "kol_selection_v3",
        "published_version_id": "version-kol",
        "scope": {"platforms": ["douyin"]},
        "evidence_manifest": [
            {
                "evidence_id": "ev-kol",
                "version_id": "version-kol",
                "run_id": "run-kol",
                "session_id": "session-kol",
                "source_path": "/data/summary/selected_count",
                "value": 1,
                "unit": "count",
                "candidate": {
                    "nickname": "脱敏达人",
                    "platform": "douyin",
                    "kol_uid": "kol-1",
                },
            }
        ],
    }
    result = {
        "case_id": "kol-selection-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "run_id": "run-kol",
        "session_id": "session-kol",
        "artifact_versions": ["version-kol"],
        "evidence_ids": ["ev-kol"],
        "scope": fixture["scope"],
        "artifacts": [
            {
                "version_id": "version-kol",
                "artifact_type": "kol_selection_v3",
                "numeric_lineage_complete": True,
                "narrative_grounded": True,
                "partial_limitations_complete": True,
                "payload": {
                    "schema_version": "kol_selection_v3",
                    "scope": {"platforms": ["douyin"], "top_limit": 1},
                    "data": {
                        "items": [
                            {
                                "nickname": "脱敏达人",
                                "platform": "douyin",
                                "kol_uid": "kol-1",
                                "score_snapshot": {
                                    "dimensions": {"followers": {"source": "evidence"}}
                                },
                            }
                        ],
                        "summary": {"selected_count": 1, "candidate_count": 1},
                    },
                },
            }
        ],
        "metrics": {"datatap_tool_calls": 0},
    }
    checks = evaluate_case(result, fixture)
    assert checks["valid_candidates"] is False
    assert checks["numeric_lineage_complete"] is False
    assert checks["narrative_grounded"] is False
    assert checks["partial_limitations_complete"] is False


def test_evidence_manifest_value_and_unit_are_bound_to_fixture_records() -> None:
    fixture, result = _structured_lineage_fixture()
    result["artifacts"][0]["evidence_manifest"] = [
        {
            "evidence_id": "ev-volume",
            "version_id": "version-brand",
            "run_id": "run-brand",
            "session_id": "session-brand",
            "source_path": "/data/overview/total_volume",
            "value": 10,
            "unit": "mentions",
        }
    ]
    assert evaluate_case(result, fixture)["numeric_lineage_complete"] is True

    tampered = deepcopy(result)
    tampered["artifacts"][0]["evidence_manifest"][0]["value"] = 99
    assert evaluate_case(tampered, fixture)["numeric_lineage_complete"] is False

    tampered = deepcopy(result)
    tampered["artifacts"][0]["evidence_manifest"][0]["unit"] = "CNY"
    assert evaluate_case(tampered, fixture)["numeric_lineage_complete"] is False


@pytest.mark.parametrize(
    ("behavior", "check", "outcome"),
    [
        ("drilldown", "drilldown_grounded", "drilldown_completed"),
        ("clarify", "clarification_no_tool_call", "clarification_requested"),
        ("refuse", "non_marketing_refused", "refused"),
    ],
)
@pytest.mark.parametrize("metrics", [None, {}, {"datatap_tool_calls": None}, {"datatap_tool_calls": True}, {"datatap_tool_calls": False}, {"datatap_tool_calls": -1}, {"datatap_tool_calls": 0.0}, {"datatap_tool_calls": "0"}, {"datatap_tool_calls": 0}])
def test_zero_datatap_requires_explicit_non_bool_integer(
    behavior: str, check: str, outcome: str, metrics: dict | None
) -> None:
    fixture = {
        "case_id": {
            "drilldown": "artifact-drilldown-v1",
            "clarify": "scope-clarification-v1",
            "refuse": "non-marketing-v1",
        }[behavior],
        "expected_behavior": behavior,
        "published_version_id": "version-brand" if behavior == "drilldown" else None,
    }
    result = {
        "case_id": fixture["case_id"],
        "runtime": "pi",
        "status": "completed",
        "outcome": outcome,
        "artifact_versions": [],
        "drilldown_version_id": "version-brand" if behavior == "drilldown" else None,
        "drilldown_grounded": True,
        "metrics": metrics,
    }
    expected = (
        isinstance(metrics, dict)
        and type(metrics.get("datatap_tool_calls")) is int
        and metrics.get("datatap_tool_calls") == 0
        and behavior != "drilldown"
    )
    assert evaluate_case(result, fixture)[check] is expected


def test_drilldown_grounding_requires_re_resolved_read_record_and_claim() -> None:
    fixture = {
        "case_id": "artifact-drilldown-v1",
        "expected_behavior": "drilldown",
        "published_version_id": "version-brand",
        "published_payload_digest": "1887a8ffbd0f7ba0d685e6a6692b0e2fb1778e8f37584bc05393f79e6f709ba5",
    }
    source_payload = {"data": {"overview": {"total_volume": 10}}}
    import hashlib
    import json

    digest = hashlib.sha256(json.dumps(source_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {
        "case_id": "artifact-drilldown-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "drilldown_completed",
        "artifact_versions": [],
        "drilldown_version_id": "version-brand",
        "drilldown_grounded": True,
        "metrics": {"datatap_tool_calls": 0},
        "source_version": {
            "version_id": "version-brand",
            "artifact_type": "brand_report_v3",
            "payload": source_payload,
            "payload_digest": digest,
        },
        "source_artifact": {
            "version_id": "version-brand",
            "artifact_type": "brand_report_v3",
            "status": "published",
            "validation_json": {"valid": True},
            "payload": source_payload,
        },
        "read_record": {
            "version_id": "version-brand",
            "artifact_type": "brand_report_v3",
            "payload_digest": digest,
            "path": "/data/overview/total_volume",
            "value": 10,
        },
        "claims": [
            {
                "path": "/data/overview/total_volume",
                "value": 10,
                "supporting_paths": ["/data/overview/total_volume"],
            }
        ],
    }
    assert evaluate_case(result, fixture)["drilldown_grounded"] is True

    unpublished = deepcopy(result)
    unpublished["source_artifact"]["status"] = "draft"
    assert evaluate_case(unpublished, fixture)["drilldown_grounded"] is False
    tampered = deepcopy(result)
    tampered["drilldown_grounded"] = True
    tampered.pop("read_record")
    tampered.pop("claims")
    assert evaluate_case(tampered, fixture)["drilldown_grounded"] is False
