"""Pi-only POC 的纯本地业务 Gate。

本模块只处理 JSON/值对象。它不加载应用配置、不连接数据库，也不创建任何
模型、MCP 或文件系统客户端；finalizer 可以在没有运行时环境的子进程中导入它。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

HARD_CHECKS = (
    "numeric_lineage_complete",
    "scope_preserved",
    "valid_candidates",
    "narrative_grounded",
    "drilldown_bound_to_version",
    "drilldown_grounded",
    "non_marketing_refused",
    "clarification_no_tool_call",
    "no_duplicate_report",
    "partial_limitations_complete",
)

_REPORT = "report"
_DRILLDOWN = "drilldown"
_CLARIFY = "clarify"
_REFUSE = "refuse"
_ALLOWED_PLATFORMS = frozenset({"xiaohongshu", "douyin", "bilibili", "weibo", "kuaishou"})
_FALSE_ALIASES = {
    "numeric_lineage_complete": ("lineage_complete",),
    "scope_preserved": ("scope_complete",),
    "valid_candidates": ("candidate_validity",),
    "narrative_grounded": ("narrative_complete",),
    "drilldown_bound_to_version": ("drilldown_version_bound",),
    "drilldown_grounded": ("drilldown_lineage_complete",),
    "no_duplicate_report": ("duplicate_report_check",),
    "partial_limitations_complete": ("limitations_complete",),
}


def _plain(value: Any) -> Any:
    """将 dataclass/Pydantic/普通对象归一为不带行为的值。"""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if is_dataclass(value):
        return _plain(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _plain(model_dump())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _plain(vars(value))
    return value


def _mapping(value: Any) -> dict[str, Any]:
    plain = _plain(value)
    return plain if isinstance(plain, dict) else {}


def _get(value: Any, *keys: str, default: Any = None) -> Any:
    current = _mapping(value)
    for key in keys:
        if key in current:
            return current[key]
    return default


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return []


def _explicit_false(result: Any, name: str) -> bool:
    direct = _get(result, name, default=None)
    if direct is False:
        return True
    checks = _get(result, "hard_checks", default={})
    if isinstance(checks, Mapping):
        if checks.get(name) is False:
            return True
        return any(checks.get(alias) is False for alias in _FALSE_ALIASES.get(name, ()))
    return False


def _explicit_value(result: Any, name: str) -> bool | None:
    direct = _get(result, name, default=None)
    if isinstance(direct, bool):
        return direct
    checks = _get(result, "hard_checks", default={})
    if isinstance(checks, Mapping):
        if isinstance(checks.get(name), bool):
            return checks[name]
        for alias in _FALSE_ALIASES.get(name, ()):
            if isinstance(checks.get(alias), bool):
                return checks[alias]
    return None


def _behavior(result: Any, fixture: Any) -> str:
    return str(
        _get(fixture, "expected_behavior", default=_get(result, "expected_behavior", default=""))
    ).strip().lower()


def _artifacts(result: Any) -> list[dict[str, Any]]:
    raw = _get(result, "artifacts", default=[])
    return [_mapping(item) for item in _as_list(raw)]


def _artifact_versions(result: Any) -> list[str]:
    versions: list[str] = []
    for value in _as_list(_get(result, "artifact_versions", default=[])):
        if isinstance(value, Mapping):
            candidate = value.get("version_id") or value.get("id")
        else:
            candidate = value
        if candidate is not None:
            versions.append(str(candidate))
    for artifact in _artifacts(result):
        candidate = artifact.get("version_id") or artifact.get("artifact_version_id")
        if candidate is not None and str(candidate) not in versions:
            versions.append(str(candidate))
    return versions


def _declared_artifact_versions(result: Any) -> set[str]:
    versions: set[str] = set()
    for value in _as_list(_get(result, "artifact_versions", default=[])):
        if isinstance(value, Mapping):
            value = value.get("version_id") or value.get("id")
        if value is not None:
            versions.add(str(value))
    return versions


def _datatap_calls(result: Any) -> int:
    metrics = _get(result, "metrics", default={})
    value = _get(metrics, "datatap_tool_calls", "datatap_calls", default=0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _observed(value: Any) -> bool:
    """判断是否提供了观测值；数值 0 是有效观测而不是缺失。"""
    if value is None:
        return False
    return not isinstance(value, str) or bool(value.strip())


def _has_content(value: Any) -> bool:
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_has_content(item) for item in value)
    if isinstance(value, Mapping):
        return any(_has_content(item) for item in value.values())
    return _observed(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scope_preserved(result: Any, fixture: Any) -> bool:
    expected = _get(fixture, "scope", "expected_scope", default=None)
    if expected is None:
        return True
    actual = _get(result, "scope", "artifact_scope", "scope_snapshot", default=None)
    return actual is not None and _canonical_json(actual) == _canonical_json(expected)


def _report_has_expected_artifact(result: Any, fixture: Any) -> bool:
    required = _get(fixture, "required_artifact_type", default=None)
    expected_version = _get(fixture, "published_version_id", "expected_version_id", default=None)
    versions = _artifact_versions(result)
    declared_versions = _declared_artifact_versions(result)
    if not versions:
        return False
    artifacts = _artifacts(result)
    if not artifacts:
        return False
    if required is None:
        return bool(artifacts)
    return any(
        artifact.get("artifact_type") == required
        and str(artifact.get("version_id") or "") in declared_versions
        and (expected_version is None or str(artifact.get("version_id")) == str(expected_version))
        for artifact in artifacts
    )


def _signal_from_artifacts(result: Any, name: str) -> bool | None:
    artifacts = _artifacts(result)
    signals = [artifact[name] for artifact in artifacts if isinstance(artifact.get(name), bool)]
    if signals:
        return all(signals)
    return None


def _evidence_ids(result: Any) -> list[str]:
    values: list[str] = []
    for source in (_get(result, "evidence_ids", default=[]),):
        for value in _as_list(source):
            if _nonempty(value):
                values.append(str(value))
    for artifact in _artifacts(result):
        for value in _as_list(artifact.get("evidence_ids", artifact.get("supporting_evidence_ids", []))):
            if _nonempty(value):
                values.append(str(value))
    return values


_MISSING = object()


def _path_key(path: Any) -> str:
    if not isinstance(path, str):
        return ""
    value = path.strip()
    value = value.removeprefix("/")
    value = value.replace("~1", "/").replace("~0", "~")
    return value


def _resolve_payload_path(payload: Any, path: Any) -> Any:
    key = _path_key(path)
    if not key:
        return _MISSING
    current = _mapping(payload)
    for part in key.split("/"):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if index < len(current):
                current = current[index]
                continue
        return _MISSING
    return current


def _canonical_fields(artifact: Any) -> dict[str, dict[str, Any]]:
    raw = _get(artifact, "canonical_data", default={})
    fields: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        for path, value in raw.items():
            entry = _mapping(value)
            if entry:
                key = _path_key(path)
                if key in fields:
                    fields["\x00duplicate_fields"] = {}
                fields[key] = entry
    elif isinstance(raw, (list, tuple)):
        for value in raw:
            entry = _mapping(value)
            path = entry.get("path") or entry.get("artifact_path")
            if path:
                key = _path_key(path)
                if key in fields:
                    fields["\x00duplicate_fields"] = {}
                fields[key] = entry
    return {path: value for path, value in fields.items() if path or path == "\x00duplicate_fields"}


def _lineage_evidence_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, str) and value.strip():
        return {value.strip()}
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            ids.add(item.strip())
        elif isinstance(item, Mapping):
            evidence_id = item.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id.strip():
                ids.add(evidence_id.strip())
    if isinstance(value, Mapping):
        for key in ("evidence_ids", "supporting_evidence_ids", "sources"):
            ids.update(_lineage_evidence_ids(value.get(key)))
        evidence_id = value.get("evidence_id")
        if isinstance(evidence_id, str) and evidence_id.strip():
            ids.add(evidence_id.strip())
    return ids


def _evidence_manifest(result: Any, fixture: Any, artifact: Any) -> dict[str, dict[str, Any]]:
    # Evidence manifest 是验收 fixture 的唯一信任根；Artifact/result 自带的副本
    # 只能作为被验证内容，不能在 fixture 缺失时自我授权。
    raw = _get(fixture, "evidence_manifest", default=None)
    if raw is None:
        return {}
    records: list[Any]
    if isinstance(raw, Mapping):
        records = [
            item
            for value in raw.values()
            for item in (value if isinstance(value, list) else [value])
        ]
    else:
        records = _as_list(raw)
    manifest: dict[str, dict[str, Any]] = {}
    for value in records:
        record = _mapping(value)
        evidence_id = record.get("evidence_id") or record.get("id")
        if isinstance(evidence_id, str) and evidence_id.strip():
            if evidence_id.strip() in manifest:
                manifest["\x00duplicate_manifest"] = {}
                continue
            manifest[evidence_id.strip()] = record
    return manifest


def _valid_evidence_ids(
    evidence_ids: set[str],
    *,
    manifest: Mapping[str, Mapping[str, Any]],
    version_id: str,
    result: Any,
    expected_paths: set[str] | None = None,
) -> bool:
    run_id = _get(result, "run_id", default=None)
    session_id = _get(result, "session_id", default=None)
    if not _nonempty(version_id) or not _nonempty(run_id) or not _nonempty(session_id):
        return False
    if not evidence_ids:
        return False
    for evidence_id in evidence_ids:
        record = manifest.get(evidence_id)
        if record is None:
            return False
        if str(record.get("version_id")) != str(version_id):
            return False
        if str(record.get("run_id")) != str(run_id):
            return False
        if str(record.get("session_id")) != str(session_id):
            return False
        if not _nonempty(record.get("source_path")):
            return False
        if expected_paths and _path_key(record.get("source_path")) not in expected_paths:
            return False
    return True


def _limitation_paths(value: Any) -> set[str]:
    paths: set[str] = set()
    for item in _as_list(value):
        record = _mapping(item)
        raw_paths = record.get("affected_paths", record.get("paths", record.get("path", [])))
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        for path in _as_list(raw_paths):
            key = _path_key(path)
            if key:
                paths.add(key)
    return paths


def _claim_valid(
    claim: Any,
    *,
    payload: Any,
    fields: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Mapping[str, Any]],
    version_id: str,
    result: Any,
) -> bool:
    record = _mapping(claim)
    path = _path_key(record.get("path"))
    if not path or path not in fields:
        return False
    actual = _resolve_payload_path(payload, path)
    if actual is _MISSING or actual != record.get("value"):
        return False
    supporting = {_path_key(value) for value in _as_list(record.get("supporting_paths"))}
    if not supporting or not supporting.issubset(fields):
        return False
    field_ids = set().union(
        *(_lineage_evidence_ids(fields[supporting_path].get("evidence_ids")) for supporting_path in supporting)
    )
    claim_ids = _lineage_evidence_ids(record.get("evidence_ids"))
    return (
        path in supporting
        and claim_ids == field_ids
        and _valid_evidence_ids(
        claim_ids,
        manifest=manifest,
        version_id=version_id,
        result=result,
        expected_paths=supporting,
        )
    )


def _validate_structured_artifact(
    result: Any, fixture: Any, artifact: Any, *, require_published: bool = True
) -> tuple[bool, bool, bool]:
    """返回 numeric lineage、narrative grounding、partial limitation 三项结构化结果。"""
    artifact_map = _mapping(artifact)
    version_id = str(artifact_map.get("version_id") or "")
    validation_json = artifact_map.get("validation_json")
    expected_version = _get(fixture, "published_version_id", "expected_version_id", default=None)
    if (
        (require_published and artifact_map.get("status") != "published")
        or not isinstance(validation_json, Mapping)
        or (require_published and validation_json.get("valid") is not True)
        or version_id not in _declared_artifact_versions(result)
        or (expected_version is not None and version_id != str(expected_version))
    ):
        return False, False, False
    payload = _get(artifact_map, "payload", "payload_json", default=None)
    fields = _canonical_fields(artifact_map)
    raw_lineage = _get(artifact_map, "field_lineage", default={})
    lineage = (
        {_path_key(path): value for path, value in raw_lineage.items()}
        if isinstance(raw_lineage, Mapping)
        else raw_lineage
    )
    manifest = _evidence_manifest(result, fixture, artifact_map)
    result_ids = _lineage_evidence_ids(_get(result, "evidence_ids", default=[]))
    if "\x00duplicate_manifest" in manifest or not _valid_evidence_ids(
        result_ids,
        manifest=manifest,
        version_id=version_id,
        result=result,
    ):
        return False, False, False
    if (
        not isinstance(payload, Mapping)
        or not fields
        or "\x00duplicate_fields" in fields
        or not isinstance(lineage, Mapping)
    ):
        return False, False, False

    numeric_ok = True
    partial_ok = True
    expected_claims: set[str] = set()
    for path, field in fields.items():
        actual = _resolve_payload_path(payload, path)
        value = field.get("value")
        availability = str(field.get("availability", "")).lower()
        field_ids = _lineage_evidence_ids(field.get("evidence_ids"))
        lineage_ids = _lineage_evidence_ids(lineage.get(path))
        single_evidence_value_mismatch = False
        if len(field_ids) == 1:
            evidence_record = manifest.get(next(iter(field_ids)), {})
            if "value" in evidence_record and evidence_record.get("value") != value:
                single_evidence_value_mismatch = True
        if (
            actual is _MISSING
            or actual != value
            or "path" not in field
            or _path_key(field.get("path")) != path
            or availability not in {"complete", "partial", "unavailable"}
            or not _nonempty(field.get("unit"))
            or single_evidence_value_mismatch
            or path not in lineage
            or (availability == "unavailable" and value is not None)
            or (availability != "unavailable" and not field_ids)
            or (lineage_ids != field_ids)
            or (field_ids and not _valid_evidence_ids(
                field_ids,
                manifest=manifest,
                version_id=version_id,
                result=result,
                expected_paths={path},
            ))
        ):
            numeric_ok = False
        if isinstance(value, (int, float)) and not isinstance(value, bool) and availability != "unavailable":
            expected_claims.add(path)
        if availability in {"partial", "unavailable"}:
            limitations = _get(artifact_map, "limitations", default=[])
            if path not in _limitation_paths(limitations):
                partial_ok = False

    raw_availability_map = _get(artifact_map, "availability", default={})
    availability_duplicate = False
    if isinstance(raw_availability_map, Mapping):
        availability_map: Any = {}
        for raw_path, declared in raw_availability_map.items():
            key = _path_key(raw_path)
            if key in availability_map:
                availability_duplicate = True
            availability_map[key] = declared
    else:
        availability_map = raw_availability_map
    if not isinstance(availability_map, Mapping) or availability_duplicate or {
        _path_key(path) for path in availability_map
    } != set(fields):
        numeric_ok = False
    else:
        for path, field in fields.items():
            declared = availability_map.get(path)
            if declared is None or str(declared).lower() != str(field.get("availability")).lower():
                numeric_ok = False

    if {
        _path_key(path) for path in lineage
    } != set(fields):
        numeric_ok = False

    field_evidence_ids = set()
    for field in fields.values():
        field_evidence_ids.update(_lineage_evidence_ids(field.get("evidence_ids")))
    if result_ids != field_evidence_ids:
        numeric_ok = False

    claims = _as_list(_get(artifact_map, "structured_claims", default=[]))
    claim_paths = set()
    claims_ok = bool(claims)
    for claim in claims:
        claim_path = _path_key(_get(claim, "path", default=None))
        claim_paths.add(claim_path)
        claims_ok = claims_ok and _claim_valid(
            claim,
            payload=payload,
            fields=fields,
            manifest=manifest,
            version_id=version_id,
            result=result,
        )
    numeric_ok = numeric_ok and expected_claims.issubset(claim_paths) and claims_ok

    narrative_claims = _as_list(_get(artifact_map, "narrative_claims", default=[]))
    narrative_ok = bool(narrative_claims) and all(
        _claim_valid(
            claim,
            payload=payload,
            fields=fields,
            manifest=manifest,
            version_id=version_id,
            result=result,
        )
        for claim in narrative_claims
    )
    return numeric_ok, narrative_ok, partial_ok


def validate_structured_artifact(
    result: Any, fixture: Any, artifact: Any, *, require_published: bool = True
) -> tuple[bool, bool, bool]:
    """公开纯值对象校验，供离线回放在 Publication 前复用。"""
    return _validate_structured_artifact(
        result, fixture, artifact, require_published=require_published
    )


def _artifact_self_reported_false(artifact: Any, name: str) -> bool:
    """自报 false 只能让门禁失败，不能让自报 true 代替结构化校验。"""
    return _mapping(artifact).get(name) is False


def _numeric_lineage(result: Any, fixture: Any) -> bool:
    if _behavior(result, fixture) != _REPORT:
        return True
    if not _report_has_expected_artifact(result, fixture):
        return False
    artifacts = _artifacts(result)
    return all(
        not _artifact_self_reported_false(artifact, "numeric_lineage_complete")
        and _validate_structured_artifact(result, fixture, artifact)[0]
        for artifact in artifacts
    )


def _valid_candidate(candidate: Any) -> bool:
    item = _mapping(candidate)
    if not _nonempty(item.get("nickname")):
        return False
    platform = str(item.get("platform") or "").strip().lower()
    if platform not in _ALLOWED_PLATFORMS or platform in {"unknown", "all"}:
        return False
    identity = item.get("kol_uid") or item.get("stable_id") or item.get("uid") or item.get("id")
    if not _nonempty(identity):
        return False
    score_inputs = item.get("score_inputs")
    if isinstance(score_inputs, Mapping) and any(_observed(value) for value in score_inputs.values()):
        return True
    for key in ("observed_score", "score_source", "score_evidence_ids", "observed_metrics"):
        if _observed(item.get(key)):
            return True
    dimensions = item.get("dimensions")
    if isinstance(dimensions, Mapping) and any(_observed(value) for value in dimensions.values()):
        return True
    snapshot = item.get("score_snapshot")
    snapshot_dimensions = _get(snapshot, "dimensions", default={})
    return isinstance(snapshot_dimensions, Mapping) and any(
        _observed(_get(dimension, "source", default=None))
        for dimension in snapshot_dimensions.values()
    )


def _valid_candidates(result: Any, fixture: Any) -> bool:
    if _explicit_false(result, "valid_candidates"):
        return False
    required = _get(fixture, "required_artifact_type", default=None)
    if required != "kol_selection_v3":
        return True
    candidates = _as_list(_get(result, "candidates", "items", default=[]))
    if not candidates:
        for artifact in _artifacts(result):
            candidates = _as_list(artifact.get("candidates", artifact.get("items", [])))
            if candidates:
                break
    return bool(candidates) and all(_valid_candidate(item) for item in candidates)


def _narrative_grounded(result: Any, fixture: Any) -> bool:
    if _behavior(result, fixture) != _REPORT:
        return True
    artifacts = _artifacts(result)
    if not _report_has_expected_artifact(result, fixture):
        return False
    return all(
        not _artifact_self_reported_false(artifact, "narrative_grounded")
        and _validate_structured_artifact(result, fixture, artifact)[1]
        for artifact in artifacts
    )


def _drilldown_bound(result: Any, fixture: Any) -> bool:
    if _explicit_false(result, "drilldown_bound_to_version"):
        return False
    if _behavior(result, fixture) != _DRILLDOWN:
        return True
    expected = _get(
        fixture,
        "published_version_id",
        "expected_version_id",
        "bound_version_id",
        default=None,
    )
    actual = _get(
        result,
        "drilldown_version_id",
        "bound_version_id",
        "source_version_id",
        default=None,
    )
    return _nonempty(expected) and _nonempty(actual) and str(expected) == str(actual)


def _drilldown_grounded(result: Any, fixture: Any) -> bool:
    if _explicit_false(result, "drilldown_grounded"):
        return False
    if _behavior(result, fixture) != _DRILLDOWN:
        return True
    grounded = _get(result, "drilldown_grounded", "grounded", default=None)
    return grounded is True and _datatap_calls(result) == 0


def _behavior_zero_side_effects(result: Any, fixture: Any, expected: str) -> bool:
    if _behavior(result, fixture) != expected:
        return True
    outcome = str(_get(result, "outcome", default="")).lower()
    expected_words = {
        _REFUSE: ("refuse", "refused", "refusal", "non_marketing"),
        _CLARIFY: ("clarif", "clarification"),
    }[expected]
    artifacts = _artifact_versions(result) or _as_list(_get(result, "artifacts", default=[]))
    return any(word in outcome for word in expected_words) and not artifacts and _datatap_calls(result) == 0


def _no_duplicate_report(result: Any) -> bool:
    if _explicit_false(result, "no_duplicate_report"):
        return False
    versions = _artifact_versions(result)
    if len(versions) != len(set(versions)):
        return False
    report_ids = _as_list(_get(result, "report_ids", "report_versions", default=[]))
    return len(report_ids) == len(set(map(str, report_ids)))


def _partial_limitations(result: Any, fixture: Any) -> bool:
    if _behavior(result, fixture) != _REPORT:
        return True
    artifacts = _artifacts(result)
    if not _report_has_expected_artifact(result, fixture):
        return False
    return all(
        not _artifact_self_reported_false(artifact, "partial_limitations_complete")
        and _validate_structured_artifact(result, fixture, artifact)[2]
        for artifact in artifacts
    )


def evaluate_case(result: Any, fixture: Any) -> dict[str, bool]:
    """以 fixture 预期行为评估单个 Pi 案例的十项硬门禁。"""
    checks = {
        "numeric_lineage_complete": _numeric_lineage(result, fixture),
        "scope_preserved": _scope_preserved(result, fixture),
        "valid_candidates": _valid_candidates(result, fixture),
        "narrative_grounded": _narrative_grounded(result, fixture),
        "drilldown_bound_to_version": _drilldown_bound(result, fixture),
        "drilldown_grounded": _drilldown_grounded(result, fixture),
        "non_marketing_refused": _behavior_zero_side_effects(result, fixture, _REFUSE),
        "clarification_no_tool_call": _behavior_zero_side_effects(result, fixture, _CLARIFY),
        "no_duplicate_report": _no_duplicate_report(result),
        "partial_limitations_complete": _partial_limitations(result, fixture),
    }
    return {name: bool(checks[name]) and not _explicit_false(result, name) for name in HARD_CHECKS}


@dataclass(frozen=True)
class Summary:
    """Gate 结果值对象；不持有数据库或运行时对象。"""

    gate: str
    hard_checks: dict[str, bool]
    results: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "hard_checks": dict(self.hard_checks),
            "results": [dict(result) for result in self.results],
        }


def _review_scores(review: Any, report_ids: Sequence[str]) -> list[float]:
    if review is None:
        return []
    reports = _get(review, "reports", default={})
    if not isinstance(reports, Mapping):
        return []
    scores: list[float] = []
    for case_id in report_ids:
        report = _mapping(reports.get(case_id))
        for key in ("factuality", "insight", "actionability", "limitations"):
            score = _get(report.get(key), "score", default=None)
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                scores.append(float(score))
            else:
                return []
    return scores


def finalize_execution(execution: Any, fixture: Any, review: Any = None) -> Summary:
    """在本地值对象上完成 exact-case、十项 hard check 和人工复核门禁。"""
    execution_map = _mapping(execution)
    fixture_items = [_mapping(item) for item in _as_list(fixture)]
    result_items = [_mapping(item) for item in _get(execution_map, "results", default=[])]
    expected_ids = [str(item.get("case_id", "")) for item in fixture_items]
    actual_ids = [str(item.get("case_id", "")) for item in result_items]
    exact = bool(fixture_items) and actual_ids == expected_ids
    if (
        execution_map.get("runtime") != "pi"
        or not exact
        or any(
            item.get("runtime") != "pi"
            or item.get("status") in {"failed", "not_run", "skipped_dependency"}
            or (item.get("status") == "completed" and not _nonempty(item.get("outcome")))
            for item in result_items
        )
    ):
        return Summary("INFRA_FAILED", {"exact_pi_cases": exact}, tuple(result_items))

    checks: dict[str, bool] = {}
    evaluated_results: list[dict[str, Any]] = []
    report_ids: list[str] = []
    all_versions: dict[str, str] = {}
    duplicate_case_ids: set[str] = set()
    for result, fixture_item in zip(result_items, fixture_items, strict=True):
        case_checks = evaluate_case(result, fixture_item)
        for name, value in case_checks.items():
            checks[f"case:{result.get('case_id')}:{name}"] = value
        if fixture_item.get("expected_behavior") == _REPORT:
            report_ids.append(str(result.get("case_id")))
        for version_id in _artifact_versions(result):
            owner = all_versions.get(version_id)
            if owner is not None and owner != str(result.get("case_id")):
                duplicate_case_ids.update({owner, str(result.get("case_id"))})
            all_versions[version_id] = str(result.get("case_id"))
        evaluated_results.append(result)
    if duplicate_case_ids:
        for case_id in duplicate_case_ids:
            checks[f"case:{case_id}:no_duplicate_report"] = False

    report_scores = _review_scores(review, report_ids)
    if report_ids and review is None:
        raise ValueError("poc_human_review_required")
    passed = bool(checks) and all(checks.values()) and (
        not report_ids or (len(report_scores) == len(report_ids) * 4 and all(score >= 3 for score in report_scores))
    )
    return Summary("PASS" if passed else "EVALUATED_FAIL", checks, tuple(evaluated_results))


def write_summary_append_once(path: Path, payload: Mapping[str, Any]) -> Path:
    """以独占创建写入 JSON；已存在目标绝不覆盖。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(_plain(payload), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


__all__ = [
    "HARD_CHECKS",
    "Summary",
    "evaluate_case",
    "finalize_execution",
    "validate_structured_artifact",
    "write_summary_append_once",
]
