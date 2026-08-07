"""Pi POC 对比 Harness 的纯本地 Gate 测试。"""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.pi_runtime_poc.comparison import (
    PocCase,
    PocCaseResult,
    assess_gate_a,
    begin_round,
    load_cases,
    write_append_only_round,
    write_case_result,
    write_round_summary,
)
from app.pi_runtime_poc.server import app


def _result(
    case_id: str,
    runtime: str,
    *,
    outcome: str = "completed",
    artifacts: tuple[str, ...] = ("version-1",),
    hard_checks: dict[str, bool] | None = None,
) -> PocCaseResult:
    return PocCaseResult(
        case_id=case_id,
        runtime=runtime,  # type: ignore[arg-type]
        run_id=f"{runtime}-{case_id}",
        outcome=outcome,
        artifact_versions=artifacts,
        evidence_ids=("evidence-1",),
        metrics={"coverage": 1.0, "mcp_parameter_validity": 1.0, "artifact_completeness": 1.0},
        diagnostic_path=f"outputs/{case_id}/{runtime}.json",
        hard_checks=hard_checks or {"no_secret": True, "lineage_complete": True},
    )


def test_load_cases_contains_six_versioned_scenarios_without_provider_output_or_secrets() -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "pi_runtime_poc" / "cases.json"

    cases = load_cases(fixture)

    assert len(cases) == 6
    assert {case.case_id for case in cases} == {
        "brand-research-v1",
        "campaign-evaluation-v1",
        "kol-selection-v1",
        "artifact-drilldown-v1",
        "scope-clarification-v1",
        "non-marketing-v1",
    }
    assert all(case.date_anchor and case.expected_behavior for case in cases)
    assert "sk-" not in fixture.read_text(encoding="utf-8")
    assert "raw_payload" not in fixture.read_text(encoding="utf-8")


def test_gate_a_fails_when_any_hard_check_fails_even_if_metrics_are_better() -> None:
    cases = (
        PocCase("brand-research-v1", "q", "2026-08-01", "report", "brand_report_v3"),
        PocCase("campaign-evaluation-v1", "q", "2026-08-01", "report", "campaign_report_v2"),
        PocCase("kol-selection-v1", "q", "2026-08-01", "report", "kol_selection_v3"),
    )
    results = tuple(
        _result(case.case_id, runtime, hard_checks={"no_secret": runtime == "current"})
        for case in cases
        for runtime in ("current", "pi")
    )

    summary = assess_gate_a(cases, results)

    assert summary["gate"] == "FAIL"
    assert summary["hard_checks"]["no_secret"] is False


def test_gate_a_passes_only_with_three_reports_and_two_improved_comparison_metrics() -> None:
    cases = (
        PocCase("brand-research-v1", "q", "2026-08-01", "report", "brand_report_v3"),
        PocCase("campaign-evaluation-v1", "q", "2026-08-01", "report", "campaign_report_v2"),
        PocCase("kol-selection-v1", "q", "2026-08-01", "report", "kol_selection_v3"),
        PocCase("scope-clarification-v1", "q", "2026-08-01", "clarify", None),
        PocCase("non-marketing-v1", "q", "2026-08-01", "refuse", None),
    )
    results = []
    for case in cases:
        outcome = "clarification_requested" if case.expected_behavior == "clarify" else "completed"
        artifacts = () if case.expected_behavior in {"clarify", "refuse"} else ("version-1",)
        current = _result(case.case_id, "current", outcome=outcome, artifacts=artifacts)
        pi_metrics = dict(current.metrics)
        pi_metrics["mcp_parameter_validity"] = 1.1
        pi_metrics["artifact_completeness"] = 1.1
        pi = PocCaseResult(
            **{**current.__dict__, "runtime": "pi", "run_id": f"pi-{case.case_id}", "metrics": pi_metrics}
        )
        results.extend((current, pi))

    summary = assess_gate_a(cases, tuple(results))

    assert summary["gate"] == "PASS"
    assert summary["improved_metric_count"] == 2
    assert summary["hard_checks"]["coverage_not_lower"] is True


def test_round_output_is_append_only_and_redacts_secret_like_strings(tmp_path: Path) -> None:
    result = _result("brand-research-v1", "pi")

    summary_path = write_append_only_round(tmp_path, "round-001", (result,))

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["results"][0]["case_id"] == "brand-research-v1"
    with pytest.raises(FileExistsError):
        write_append_only_round(tmp_path, "round-001", (result,))
    leaking = PocCaseResult(**{**result.__dict__, "diagnostic_path": "sk-should-not-write"})
    with pytest.raises(ValueError, match="poc_output_contains_secret"):
        write_append_only_round(tmp_path, "round-002", (leaking,))


def test_started_round_writes_one_case_per_runtime_then_one_summary(tmp_path: Path) -> None:
    result = _result("brand-research-v1", "pi")
    round_dir = begin_round(tmp_path, "round-003")

    case_path = write_case_result(round_dir, result)
    summary_path = write_round_summary(round_dir, (result,), {"gate": "FAIL"})

    assert case_path == round_dir / "brand-research-v1" / "pi.json"
    assert case_path.exists()
    assert summary_path.exists()
    with pytest.raises(FileExistsError):
        write_case_result(round_dir, result)
    with pytest.raises(FileExistsError):
        begin_round(tmp_path, "round-003")


def test_poc_internal_server_only_exposes_pi_callback_routes_without_main_lifespan() -> None:
    assert app.url_path_for("healthz") == "/healthz"
    assert (
        app.url_path_for("execute_internal_tool", run_id="test-run")
        == "/api/v1/internal/pi-poc/runs/test-run/internal-tools"
    )
    assert not hasattr(app.state, "agent_executor")


def test_settings_accepts_blank_legacy_endpoint_but_requires_explicit_endpoint_mapping() -> None:
    settings = Settings(
        mysql_password=SecretStr("test-only-password"),
        jwt_secret=SecretStr("test-only-jwt-secret-at-least-32-characters"),
        tencent_plan_api_key=SecretStr("test-only-model-key"),
        datatap_mcp_token=SecretStr("test-only-datatap-token"),
        datatap_mcp_url="",
        datatap_mcp_urls={"insight-cube-mcp": "https://datatap.example.test/insight/mcp"},
    )

    assert settings.datatap_mcp_url is None
    assert str(settings.datatap_mcp_urls["insight-cube-mcp"]) == "https://datatap.example.test/insight/mcp"
