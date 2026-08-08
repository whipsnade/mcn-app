"""Marketing Capability Pack B0 的确定性离线回放。

回放只读取脱敏 JSON fixture，重建 Pi execution 值对象；不创建数据库、模型、MCP
客户端或钱包对象。它用于证明六类行为的形状和 Gate 输入，而不是替代真实 UAT。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.pi_runtime_poc.gate import evaluate_case

_CASE_IDS = (
    "brand-research-v1",
    "campaign-evaluation-v1",
    "kol-selection-v1",
    "artifact-drilldown-v1",
    "scope-clarification-v1",
    "non-marketing-v1",
)
_SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9._-]+|Bearer\s+\S+|(?:api[_-]?key|password|token)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if _SECRET_PATTERN.search(text):
        raise ValueError("offline_replay_fixture_contains_secret")
    return json.loads(text)


def _digest(payloads: dict[str, Any]) -> str:
    serialized = _canonical(payloads).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _require_list(value: Any, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(code)
    return value


def _validate_events(events: Any, case_ids: list[str]) -> None:
    if not isinstance(events, dict) or list(events) != case_ids:
        raise ValueError("offline_replay_event_manifest_invalid")
    for case_id in case_ids:
        sequence = events.get(case_id)
        if (
            not isinstance(sequence, list)
            or not sequence
            or sequence[0] != "run.started"
            or sequence[-1] != "run.completed"
            or not all(isinstance(event, str) for event in sequence)
        ):
            raise ValueError("offline_replay_event_sequence_invalid")


def _validate_cases_and_results(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    case_ids = [str(case.get("case_id", "")) for case in cases]
    result_ids = [str(result.get("case_id", "")) for result in results]
    if case_ids != list(_CASE_IDS) or result_ids != case_ids:
        raise ValueError("offline_replay_case_order_invalid")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("offline_replay_case_duplicate")

    versions: set[str] = set()
    for case, result in zip(cases, results, strict=True):
        if result.get("runtime") != "pi" or result.get("status") != "completed":
            raise ValueError("offline_replay_result_not_pi_completed")
        behavior = case.get("expected_behavior")
        outcome = str(result.get("outcome", "")).lower()
        if behavior == "clarify" and "clarif" not in outcome:
            raise ValueError("offline_replay_clarification_outcome_invalid")
        if behavior == "refuse" and not any(word in outcome for word in ("refuse", "refusal")):
            raise ValueError("offline_replay_refusal_outcome_invalid")
        for version in result.get("artifact_versions", []):
            version_id = str(version)
            if version_id in versions:
                raise ValueError("offline_replay_duplicate_report")
            versions.add(version_id)
        if behavior in {"clarify", "refuse", "drilldown"} and (
            result.get("artifact_versions")
            or result.get("metrics", {}).get("datatap_tool_calls") != 0
        ):
            raise ValueError("offline_replay_non_report_side_effect")
        required_type = case.get("required_artifact_type")
        if behavior == "report":
            artifacts = result.get("artifacts")
            if not result.get("artifact_versions") or not isinstance(artifacts, list):
                raise ValueError("offline_replay_artifact_missing")
            if not any(
                item.get("artifact_type") == required_type
                and item.get("status") == "published"
                and isinstance(item.get("validation_json"), dict)
                and item["validation_json"].get("valid") is True
                for item in artifacts
            ):
                raise ValueError("offline_replay_artifact_type_mismatch")
        if behavior == "drilldown" and result.get("drilldown_version_id") != case.get(
            "published_version_id"
        ):
            raise ValueError("offline_replay_drilldown_version_mismatch")


@dataclass(frozen=True)
class OfflineExecution:
    runtime: str
    results: tuple[dict[str, Any], ...]
    fixture_digest: str
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "results": [dict(result) for result in self.results],
            "fixture_digest": self.fixture_digest,
            "summary": dict(self.summary),
        }


def run_offline_marketing_replay(fixtures: Path) -> OfflineExecution:
    """读取固定 fixture 并返回可供 Gate 使用的六案例 execution。"""
    root = Path(fixtures).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    payloads = {
        name: _read_json(root / name)
        for name in ("cases.json", "results.json", "events.json")
    }
    cases = _require_list(payloads["cases.json"], "offline_replay_cases_invalid")
    results = _require_list(payloads["results.json"], "offline_replay_results_invalid")
    case_ids = [str(case.get("case_id", "")) for case in cases]
    _validate_cases_and_results(cases, results)
    _validate_events(payloads["events.json"], case_ids)
    hard_checks = {
        f"case:{result['case_id']}:{name}": value
        for case, result in zip(cases, results, strict=True)
        for name, value in evaluate_case(result, case).items()
    }
    if not hard_checks or not all(hard_checks.values()):
        raise ValueError("offline_replay_hard_check_failed")
    summary = {
        "schema": "offline_marketing_b0_summary_v1",
        "hard_check_gate": "PASS",
        "human_review": "not_provided",
        "hard_checks": hard_checks,
    }
    return OfflineExecution("pi", tuple(results), _digest(payloads), summary)


__all__ = ["OfflineExecution", "run_offline_marketing_replay"]
