"""Marketing Capability Pack B0 的确定性离线业务回放。

回放从 synthetic Evidence 和 fake Pi event 对象重新构建 Artifact，依次执行
Builder、Validator、Publication、Version 和 Exporter。fixture 不保存 hard-check
结论，也不创建数据库、模型、MCP 客户端或钱包对象。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.pi_runtime_poc.gate import evaluate_case, validate_structured_artifact

_CASE_IDS = (
    "brand-research-v1",
    "campaign-evaluation-v1",
    "kol-selection-v1",
    "artifact-drilldown-v1",
    "scope-clarification-v1",
    "non-marketing-v1",
)
_EXPECTED_SOURCE_PATHS = {
    "evidence-brand-volume": "/data/overview/total_volume",
    "evidence-campaign-volume": "/data/overview/total_volume",
    "evidence-campaign-engagement": "/data/overview/total_engagement",
    "evidence-kol-profile": "/data/selection/selected_count",
}


class _ReplayInputError(ValueError):
    """保持离线 fixture 输入错误的 ValueError 兼容，同时保留稳定 code。"""
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
    return hashlib.sha256(_canonical(payloads).encode("utf-8")).hexdigest()


def _require_list(value: Any, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(code)
    return value


def _event_type(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("type", ""))
    return ""


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
        if not isinstance(sequence, list) or not sequence:
            raise ValueError("offline_replay_event_sequence_invalid")
        if _event_type(sequence[0]) != "run.started" or _event_type(sequence[-1]) != "run.completed":
            raise ValueError("offline_replay_event_sequence_invalid")
        if not all(isinstance(event, dict) for event in sequence):
            raise ValueError("offline_replay_event_shape_invalid")
        case_behavior = "report"
        if case_id == "artifact-drilldown-v1":
            case_behavior = "drilldown"
        elif case_id == "scope-clarification-v1":
            case_behavior = "clarify"
        elif case_id == "non-marketing-v1":
            case_behavior = "refuse"
        types = tuple(_event_type(event) for event in sequence)
        if types != required_types[case_behavior]:
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
        expected_path = _EXPECTED_SOURCE_PATHS.get(evidence_id)
        if expected_path is not None and record.get("source_path") != expected_path:
            raise ValueError("offline_replay_evidence_path_invalid")
        result[evidence_id] = record
    return result


def _event(sequence: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    matches = [event for event in sequence if _event_type(event) == event_type]
    if len(matches) != 1:
        raise ValueError("offline_replay_event_duplicate")
    return matches[0]


def _scope(case: dict[str, Any]) -> dict[str, Any]:
    value = case.get("scope")
    if not isinstance(value, dict):
        raise _ReplayInputError("offline_replay_scope_invalid")
    return json.loads(_canonical(value))


def _manifest(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(_canonical(record)) for record in records.values()]


def _field(path: str, value: Any, evidence_ids: list[str], unit: str) -> dict[str, Any]:
    return {
        "value": value,
        "availability": "complete",
        "evidence_ids": list(evidence_ids),
        "unit": unit,
        "path": "/" + path,
    }


def _build_report(
    case: dict[str, Any], sequence: list[dict[str, Any]], records: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = _event(sequence, "run.started")
    settled = _event(sequence, "evidence.settled")
    built = _event(sequence, "artifact.build")
    narrative = _event(sequence, "narrative.claim")
    version_id = str(built.get("version_id", ""))
    artifact_type = str(built.get("artifact_type", ""))
    run_id = str(started.get("run_id", ""))
    session_id = str(started.get("session_id", ""))
    evidence_ids = settled.get("evidence_ids")
    if (
        not version_id
        or artifact_type != case.get("required_artifact_type")
        or not isinstance(evidence_ids, list)
        or not evidence_ids
    ):
        raise ValueError("offline_replay_builder_input_invalid")
    selected = []
    expected_manifest = {
        str(item.get("evidence_id")): item
        for item in case.get("evidence_manifest", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    for evidence_id in evidence_ids:
        if evidence_id not in records:
            raise ValueError("offline_replay_evidence_unknown")
        record = records[evidence_id]
        if (
            str(record.get("version_id")) != version_id
            or str(record.get("run_id")) != run_id
            or str(record.get("session_id")) != session_id
        ):
            raise ValueError("offline_replay_evidence_scope_invalid")
        expected = expected_manifest.get(str(evidence_id))
        if expected is None or (
            "value" in expected and record.get("value") != expected.get("value")
        ):
            raise ValueError("offline_replay_evidence_value_invalid")
        selected.append(record)

    if case["case_id"] == "brand-research-v1":
        fields = {
            "data/overview/total_volume": _field(
                "data/overview/total_volume", selected[0].get("value"), [evidence_ids[0]], "mentions"
            )
        }
    elif case["case_id"] == "campaign-evaluation-v1":
        if len(selected) < 2:
            raise ValueError("offline_replay_evidence_incomplete")
        fields = {
            "data/overview/total_volume": _field(
                "data/overview/total_volume", selected[0].get("value"), [evidence_ids[0]], "posts"
            ),
            "data/overview/total_engagement": _field(
                "data/overview/total_engagement", selected[1].get("value"), [evidence_ids[1]], "interactions"
            ),
        }
    else:
        fields = {
            "data/selection/selected_count": _field(
                "data/selection/selected_count", selected[0].get("value"), [evidence_ids[0]], "count"
            )
        }

    build_path = str(built.get("path", ""))
    if build_path not in fields:
        raise ValueError("offline_replay_builder_path_invalid")
    expected_value = fields[build_path]["value"]
    if built.get("value") != expected_value:
        raise ValueError("offline_replay_builder_value_mismatch")
    narrative_path = str(narrative.get("path", ""))
    if narrative_path != "/" + build_path or narrative.get("value") != expected_value:
        raise ValueError("offline_replay_narrative_input_invalid")

    payload_data: dict[str, Any] = {}
    for path, field in fields.items():
        current = payload_data
        parts = path.split("/")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = field["value"]
    field_lineage = {path: {"evidence_ids": field["evidence_ids"]} for path, field in fields.items()}
    claims = [
        {
            "path": "/" + path,
            "value": field["value"],
            "supporting_paths": [path],
            "evidence_ids": field["evidence_ids"],
        }
        for path, field in fields.items()
    ]
    artifact = {
        "version_id": version_id,
        "artifact_type": artifact_type,
        "status": "draft",
        "validation_json": {"valid": False},
        "payload": payload_data,
        "canonical_data": fields,
        "field_lineage": field_lineage,
        "structured_claims": claims,
        "narrative_claims": [
            {
                "path": claim["path"],
                "value": claim["value"],
                "supporting_paths": claim["supporting_paths"],
                "evidence_ids": claim["evidence_ids"],
            }
            for claim in claims
        ],
        "availability": {path: field["availability"] for path, field in fields.items()},
        "limitations": [],
        "evidence_manifest": _manifest(records),
    }
    result = {
        "case_id": case["case_id"],
        "runtime": "pi",
        "status": "completed",
        "outcome": "completed",
        "run_id": run_id,
        "session_id": session_id,
        "artifact_versions": [version_id],
        "evidence_ids": list(evidence_ids),
        "evidence_manifest": _manifest(records),
        "scope": _scope(case),
        "artifacts": [artifact],
        "metrics": {"datatap_tool_calls": 0},
    }
    if case["case_id"] == "kol-selection-v1":
        candidate = selected[0].get("candidate")
        if not isinstance(candidate, dict):
            raise ValueError("offline_replay_candidate_missing")
        result["candidates"] = [json.loads(_canonical(candidate))]
    return result, artifact


def _publish_and_export(
    result: dict[str, Any], fixture: dict[str, Any], artifact: dict[str, Any], export_event: dict[str, Any]
) -> None:
    numeric_ok, narrative_ok, partial_ok = validate_structured_artifact(
        result, fixture, artifact, require_published=False
    )
    if not all((numeric_ok, narrative_ok, partial_ok)):
        raise ValueError("offline_replay_validator_failed")
    artifact["status"] = "published"
    artifact["validation_json"] = {"valid": True}
    if artifact.get("status") != "published" or artifact["validation_json"].get("valid") is not True:
        raise ValueError("offline_replay_publication_failed")
    if export_event.get("format", "json") != "json":
        raise ValueError("offline_replay_export_format_invalid")
    exported = _export_artifact(artifact, export_event)
    if not exported:
        raise ValueError("offline_replay_export_empty")
    artifact["exported"] = True
    artifact["export_digest"] = hashlib.sha256(exported).hexdigest()


def _export_artifact(artifact: dict[str, Any], export_event: dict[str, Any]) -> bytes:
    """离线 Exporter：只序列化已发布 payload，返回确定性 JSON bytes。"""
    if export_event.get("artifact_type", artifact.get("artifact_type")) != artifact.get("artifact_type"):
        raise ValueError("offline_replay_export_artifact_invalid")
    if artifact.get("status") != "published" or artifact.get("validation_json", {}).get("valid") is not True:
        raise ValueError("offline_replay_export_before_publication")
    return _canonical(artifact["payload"]).encode("utf-8")


def _execute_case(
    case: dict[str, Any], sequence: list[dict[str, Any]], evidence: Any, prior: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    case_id = str(case.get("case_id"))
    records = _evidence_by_id(evidence, case_id)
    started = _event(sequence, "run.started")
    result_base = {
        "case_id": case_id,
        "runtime": "pi",
        "status": "completed",
        "run_id": started.get("run_id"),
        "session_id": started.get("session_id"),
        "artifact_versions": [],
        "evidence_ids": [],
        "metrics": {"datatap_tool_calls": 0},
    }
    behavior = case.get("expected_behavior")
    if behavior == "report":
        result, artifact = _build_report(case, sequence, records)
        publish_event = _event(sequence, "artifact.publish")
        export_event = _event(sequence, "exporter.export")
        if publish_event.get("version_id", artifact["version_id"]) != artifact["version_id"]:
            raise ValueError("offline_replay_publication_version_invalid")
        if export_event.get("format", "json") != "json":
            raise ValueError("offline_replay_export_format_invalid")
        _publish_and_export(result, case, artifact, export_event)
        result["outcome"] = "completed"
        return result
    if behavior == "drilldown":
        read = _event(sequence, "artifact.read")
        version_id = str(read.get("version_id", ""))
        if version_id != str(case.get("published_version_id")) or version_id not in prior:
            raise ValueError("offline_replay_drilldown_version_mismatch")
        result_base.update(
            {"outcome": "drilldown_completed", "drilldown_version_id": version_id, "drilldown_grounded": True}
        )
        return result_base
    if behavior == "clarify":
        result_base["outcome"] = "clarification_requested"
        return result_base
    if behavior == "refuse":
        result_base["outcome"] = "refused"
        return result_base
    raise ValueError("offline_replay_case_behavior_invalid")


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
    """执行六案例 synthetic Evidence 回放并由 Gate 计算 hard-check 结果。"""
    root = Path(fixtures).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    payloads = {
        name: _read_json(root / name)
        for name in ("cases.json", "evidence.json", "events.json")
    }
    cases = _require_list(payloads["cases.json"], "offline_replay_cases_invalid")
    evidence = payloads["evidence.json"]
    if not isinstance(evidence, dict) or list(evidence) != list(_CASE_IDS):
        raise ValueError("offline_replay_evidence_manifest_invalid")
    case_ids = [str(case.get("case_id", "")) for case in cases]
    _validate_cases(cases)
    _validate_events(payloads["events.json"], case_ids)
    results: list[dict[str, Any]] = []
    prior_versions: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = str(case["case_id"])
        result = _execute_case(case, payloads["events.json"][case_id], evidence, prior_versions)
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
        "schema": "offline_marketing_b0_summary_v2",
        "hard_check_gate": "PASS",
        "human_review": "not_provided",
        "hard_checks": hard_checks,
    }
    return OfflineExecution("pi", tuple(results), _digest(payloads), summary)


__all__ = ["OfflineExecution", "run_offline_marketing_replay"]
