"""Marketing Capability Pack B0 的确定性离线回放驱动器。

本文件只负责读取 fixture、校验事件顺序并驱动
``app.agent_artifacts.offline_pipeline``；Builder、Validator、Publication 和
Exporter 均来自共享生产实现。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent_artifacts.offline_pipeline import (
    build_artifact,
    canonical_json,
    payload_digest,
    publish_and_export,
)
from app.marketing_capability_pack import runtime as capability_runtime
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


def _read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if _SECRET_PATTERN.search(text):
        raise ValueError("offline_replay_fixture_contains_secret")
    return json.loads(text)


def _require_list(value: Any, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(code)
    return value


def _event_type(event: Any) -> str:
    return str(event.get("type", "")) if isinstance(event, dict) else ""


def _event(sequence: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    matches = [event for event in sequence if _event_type(event) == event_type]
    if len(matches) != 1:
        raise ValueError("offline_replay_event_duplicate")
    return matches[0]


def _validate_events(events: Any, case_ids: list[str]) -> None:
    if not isinstance(events, dict) or list(events) != case_ids:
        raise ValueError("offline_replay_event_manifest_invalid")
    required_types = {
        "report": (
            "run.started",
            "evidence.settled",
            "artifact.build",
            "narrative.claim",
            "artifact.publish",
            "exporter.export",
            "run.completed",
        ),
        "drilldown": ("run.started", "artifact.read", "run.completed"),
        "clarify": ("run.started", "clarification.requested", "run.completed"),
        "refuse": ("run.started", "refusal.completed", "run.completed"),
    }
    for case_id in case_ids:
        sequence = events.get(case_id)
        if not isinstance(sequence, list) or not sequence or not all(
            isinstance(event, dict) for event in sequence
        ):
            raise ValueError("offline_replay_event_sequence_invalid")
        if _event_type(sequence[0]) != "run.started" or _event_type(sequence[-1]) != "run.completed":
            raise ValueError("offline_replay_event_sequence_invalid")
        behavior = "report"
        if case_id == "artifact-drilldown-v1":
            behavior = "drilldown"
        elif case_id == "scope-clarification-v1":
            behavior = "clarify"
        elif case_id == "non-marketing-v1":
            behavior = "refuse"
        if tuple(_event_type(event) for event in sequence) != required_types[behavior]:
            raise ValueError("offline_replay_event_sequence_invalid")


def _validate_cases(cases: list[dict[str, Any]]) -> None:
    case_ids = [str(case.get("case_id", "")) for case in cases]
    if case_ids != list(_CASE_IDS):
        raise ValueError("offline_replay_case_order_invalid")
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("offline_replay_case_duplicate")
    for case in cases:
        if case.get("expected_behavior") not in {"report", "drilldown", "clarify", "refuse"}:
            raise ValueError("offline_replay_case_behavior_invalid")
        if case.get("scope") != case.get("expected_scope", case.get("scope")):
            raise ValueError("offline_replay_scope_invalid")


def _evidence_by_id(evidence: Any, case_id: str) -> dict[str, dict[str, Any]]:
    records = evidence.get(case_id) if isinstance(evidence, dict) else None
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("offline_replay_evidence_shape_invalid")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        evidence_id = record.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id in result:
            raise ValueError("offline_replay_evidence_identity_invalid")
        if not all(record.get(key) for key in ("version_id", "run_id", "session_id", "source_path")):
            raise ValueError("offline_replay_evidence_metadata_invalid")
        result[evidence_id] = record
    return result


def _manifest(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(canonical_json(record)) for record in records.values()]


def _execute_case(
    case: dict[str, Any],
    sequence: list[dict[str, Any]],
    evidence: Any,
    prior: dict[str, dict[str, Any]],
    capability: Any,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    started = _event(sequence, "run.started")
    completed = _event(sequence, "run.completed")
    behavior = case.get("expected_behavior")
    base: dict[str, Any] = {
        "case_id": case_id,
        "runtime": "pi",
        "status": "completed",
        "run_id": started.get("run_id"),
        "session_id": started.get("session_id"),
        "artifact_versions": [],
        "evidence_ids": [],
        "evidence_manifest": [],
        "scope": case.get("scope"),
        "metrics": {"datatap_tool_calls": 0},
    }
    expected_outcome = {
        "report": "completed",
        "drilldown": "drilldown_completed",
        "clarify": "clarification_requested",
        "refuse": "refused",
    }[str(behavior)]
    if completed.get("outcome") != expected_outcome:
        raise ValueError("offline_replay_run_outcome_invalid")

    if behavior == "report":
        records = _evidence_by_id(evidence, case_id)
        settled = _event(sequence, "evidence.settled")
        selected_ids = settled.get("evidence_ids")
        if not isinstance(selected_ids, list) or not selected_ids:
            raise ValueError("offline_replay_evidence_unknown")
        selected = {evidence_id: records.get(evidence_id) for evidence_id in selected_ids}
        if any(record is None for record in selected.values()):
            raise ValueError("offline_replay_evidence_unknown")
        for evidence_id, record in selected.items():
            if (
                str(record.get("version_id")) != str(_event(sequence, "artifact.build").get("version_id"))
                or str(record.get("run_id")) != str(started.get("run_id"))
                or str(record.get("session_id")) != str(started.get("session_id"))
            ):
                raise ValueError("offline_replay_evidence_scope_invalid")
            expected = next(
                (
                    item
                    for item in case.get("evidence_manifest", [])
                    if isinstance(item, dict) and item.get("evidence_id") == evidence_id
                ),
                None,
            )
            if expected is None or any(
                key in expected and expected.get(key) != record.get(key)
                for key in ("value", "unit", "source_path")
            ):
                raise ValueError("offline_replay_evidence_value_invalid")
        built = build_artifact(case=case, sequence=sequence, records=selected, capability=capability)
        publish_event = _event(sequence, "artifact.publish")
        export_event = _event(sequence, "exporter.export")
        publish_and_export(built=built, publish_event=publish_event, export_event=export_event)
        artifact = built.artifact
        base.update(
            {
                "outcome": "completed",
                "artifact_versions": [artifact["version_id"]],
                "evidence_ids": list(selected_ids),
                "evidence_manifest": _manifest(selected),
                "artifacts": [artifact],
            }
        )
        if artifact.get("artifact_type") == "kol_selection_v3":
            base["candidates"] = list(artifact["payload"]["data"]["items"])
        return base

    if behavior == "drilldown":
        read_event = _event(sequence, "artifact.read")
        version_id = str(read_event.get("version_id") or "")
        source_result = prior.get(version_id)
        if version_id != str(case.get("published_version_id")) or source_result is None:
            raise ValueError("offline_replay_drilldown_version_mismatch")
        artifacts = source_result.get("artifacts") or []
        if len(artifacts) != 1:
            raise ValueError("offline_replay_drilldown_version_mismatch")
        source_artifact = artifacts[0]
        source_payload = source_artifact.get("payload")
        path = read_event.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("offline_replay_drilldown_path_invalid")
        value: Any = source_payload
        for part in path.removeprefix("/").split("/"):
            if not isinstance(value, dict) or part not in value:
                raise ValueError("offline_replay_drilldown_path_invalid")
            value = value[part]
        digest = payload_digest(source_payload)
        if read_event.get("payload_digest") != digest:
            raise ValueError("offline_replay_drilldown_digest_invalid")
        if read_event.get("artifact_type") != source_artifact.get("artifact_type"):
            raise ValueError("offline_replay_drilldown_artifact_invalid")
        claim = {
            "path": path,
            "value": value,
            "supporting_paths": [path],
        }
        base.update(
            {
                "outcome": "drilldown_completed",
                "drilldown_version_id": version_id,
                "source_version": {
                    "version_id": version_id,
                    "artifact_type": source_artifact.get("artifact_type"),
                    "payload": source_payload,
                    "payload_digest": digest,
                },
                "source_artifact": source_artifact,
                "read_record": {
                    "version_id": version_id,
                    "artifact_type": source_artifact.get("artifact_type"),
                    "payload_digest": digest,
                    "path": path,
                    "value": value,
                },
                "claims": [claim],
            }
        )
        return base

    event_type = "clarification.requested" if behavior == "clarify" else "refusal.completed"
    event = _event(sequence, event_type)
    if event.get("outcome") != expected_outcome:
        raise ValueError("offline_replay_behavior_outcome_invalid")
    base["outcome"] = expected_outcome
    return base


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
    """执行六案例 synthetic Evidence 回放，并由 Gate 计算 hard checks。"""
    root = Path(fixtures).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    payloads = {
        name: _read_json(root / name) for name in ("cases.json", "evidence.json", "events.json")
    }
    cases = _require_list(payloads["cases.json"], "offline_replay_cases_invalid")
    evidence = payloads["evidence.json"]
    if not isinstance(evidence, dict) or list(evidence) != list(_CASE_IDS):
        raise ValueError("offline_replay_evidence_manifest_invalid")
    case_ids = [str(case.get("case_id", "")) for case in cases]
    _validate_cases(cases)
    _validate_events(payloads["events.json"], case_ids)
    capability = capability_runtime.build_marketing_run_capability(model_version="offline-replay")
    results: list[dict[str, Any]] = []
    prior_versions: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = str(case["case_id"])
        result = _execute_case(case, payloads["events.json"][case_id], evidence, prior_versions, capability)
        results.append(result)
        for version_id in result.get("artifact_versions", []):
            if version_id in prior_versions:
                raise ValueError("offline_replay_duplicate_report")
            prior_versions[str(version_id)] = result
    hard_checks = {
        f"case:{result['case_id']}:{name}": value
        for case, result in zip(cases, results, strict=True)
        for name, value in evaluate_case(result, case).items()
    }
    if not hard_checks or not all(hard_checks.values()):
        raise ValueError("offline_replay_hard_check_failed")
    summary = {
        "schema": "offline_marketing_b0_summary_v3",
        "hard_check_gate": "PASS",
        "human_review": "not_provided",
        "hard_checks": hard_checks,
    }
    return OfflineExecution("pi", tuple(results), hashlib.sha256(canonical_json(payloads).encode()).hexdigest(), summary)


__all__ = ["OfflineExecution", "run_offline_marketing_replay"]
