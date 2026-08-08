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
        "scope": {"brand": "某品牌", "platforms": ["xiaohongshu"]},
    }


def _report_result() -> dict:
    return {
        "case_id": "brand-research-v1",
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "run_id": "run-brand",
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
        result["artifacts"][0]["numeric_lineage_complete"] = False
    elif field == "narrative_grounded":
        result["artifacts"][0]["narrative_grounded"] = False
    elif field == "partial_limitations_complete":
        result["artifacts"][0]["partial_limitations_complete"] = False
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
    assert checks["drilldown_grounded"]

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
