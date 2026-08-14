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
_KOL_REQUIRED_AVAILABILITY_SECTIONS = frozenset({"scoring", "items", "summary"})
_KOL_AVAILABILITY_STATUSES = frozenset({"complete", "partial", "unavailable"})
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


def _raw_artifact_version_ids(result: Any) -> tuple[list[str], list[str]]:
    """返回未去重的声明 Version 与 Artifact Version 列表。"""
    declared: list[str] = []
    for value in _as_list(_get(result, "artifact_versions", default=[])):
        candidate = value.get("version_id") or value.get("id") if isinstance(value, Mapping) else value
        if candidate is not None:
            declared.append(str(candidate))
    artifacts = [
        str(artifact.get("version_id") or artifact.get("artifact_version_id"))
        for artifact in _artifacts(result)
        if artifact.get("version_id") is not None or artifact.get("artifact_version_id") is not None
    ]
    return declared, artifacts


def _declared_artifact_versions(result: Any) -> set[str]:
    versions: set[str] = set()
    for value in _as_list(_get(result, "artifact_versions", default=[])):
        if isinstance(value, Mapping):
            value = value.get("version_id") or value.get("id")
        if value is not None:
            versions.add(str(value))
    return versions


def _datatap_calls(result: Any) -> int | None:
    metrics = _get(result, "metrics", default={})
    if not isinstance(metrics, Mapping):
        return None
    if "datatap_tool_calls" in metrics:
        value = metrics["datatap_tool_calls"]
    elif "datatap_calls" in metrics:
        value = metrics["datatap_calls"]
    else:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


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


def _scope_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(key in actual and _scope_subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, (list, tuple)):
        return isinstance(actual, (list, tuple)) and list(actual) == list(expected)
    return actual == expected


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


def _resolve_narrative_path(payload: Any, path: Any) -> Any:
    value = _resolve_payload_path(payload, path)
    if value is not _MISSING:
        return value
    if not isinstance(path, str) or "." not in path:
        return _MISSING
    return _resolve_payload_path(payload, "/" + path.replace(".", "/"))


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


def _expected_fields(fixture: Any) -> dict[str, dict[str, Any]]:
    raw = _get(fixture, "expected_fields", default={})
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path, value in raw.items():
        entry = _mapping(value)
        key = _path_key(path)
        if key and entry:
            result[key] = entry
    return result


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


def _actual_evidence_manifest(result: Any, artifact: Any) -> dict[str, dict[str, Any]]:
    """读取结果携带的 Evidence 副本；fixture 仍是唯一信任根。"""
    manifest: dict[str, dict[str, Any]] = {}
    duplicate = False
    for raw in (
        _get(artifact, "evidence_manifest", default=[]),
        _get(result, "evidence_manifest", default=[]),
    ):
        records = raw.values() if isinstance(raw, Mapping) else _as_list(raw)
        source_ids: set[str] = set()
        for value in records:
            if isinstance(value, Mapping) and not isinstance(value, dict):
                value = dict(value)
            record = _mapping(value)
            evidence_id = record.get("evidence_id") or record.get("id")
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                continue
            key = evidence_id.strip()
            if key in source_ids:
                duplicate = True
                continue
            source_ids.add(key)
            previous = manifest.get(key)
            if previous is not None and _canonical_json(previous) != _canonical_json(record):
                duplicate = True
            else:
                manifest[key] = record
    if duplicate:
        manifest["\x00duplicate_actual_manifest"] = {}
    return manifest


def _valid_evidence_ids(
    evidence_ids: set[str],
    *,
    manifest: Mapping[str, Mapping[str, Any]],
    version_id: str,
    result: Any,
    expected_paths: set[str] | None = None,
    actual_manifest: Mapping[str, Mapping[str, Any]] | None = None,
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
        if actual_manifest is not None:
            actual = actual_manifest.get(evidence_id)
            if actual is None or evidence_id.startswith("\x00"):
                return False
            for key in (
                "version_id",
                "run_id",
                "session_id",
                "source_path",
                "value",
                "unit",
            ):
                if key in record and actual.get(key) != record.get(key):
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


def _normalize_section_path(path: Any) -> str:
    key = _path_key(path).replace(".", "/")
    if key and not key.startswith("data/"):
        key = f"data/{key}"
    return key


def _claim_valid(
    claim: Any,
    *,
    payload: Any,
    fields: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Mapping[str, Any]],
    version_id: str,
    result: Any,
    source_paths_by_path: Mapping[str, Any] | None = None,
    actual_manifest: Mapping[str, Mapping[str, Any]] | None = None,
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
    expected_source_paths = {
        _path_key(value)
        for value in _as_list((source_paths_by_path or {}).get(path))
        if _path_key(value)
    }
    return (
        path in supporting
        and claim_ids == field_ids
        and _valid_evidence_ids(
        claim_ids,
        manifest=manifest,
        version_id=version_id,
        result=result,
        expected_paths=expected_source_paths or supporting,
        actual_manifest=actual_manifest,
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
    actual_manifest = _actual_evidence_manifest(result, artifact_map)
    raw_source_paths = _mapping(artifact_map.get("lineage_source_paths", {}))
    source_paths_by_path = {
        _path_key(path): value for path, value in raw_source_paths.items()
    }
    result_ids = _lineage_evidence_ids(_get(result, "evidence_ids", default=[]))
    if (
        "\x00duplicate_manifest" in manifest
        or "\x00duplicate_actual_manifest" in actual_manifest
        or not _valid_evidence_ids(
        result_ids,
        manifest=manifest,
        version_id=version_id,
        result=result,
        actual_manifest=actual_manifest,
        )
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
        expected_field = _expected_fields(fixture).get(path, {})
        expected_ids = _lineage_evidence_ids(expected_field.get("evidence_ids"))
        expected_source_paths = {
            _path_key(item)
            for item in _as_list(expected_field.get("source_paths"))
            if _path_key(item)
        }
        if not expected_source_paths:
            expected_source_paths = {
                _path_key(item)
                for item in _as_list(expected_field.get("derivation_inputs"))
                if _path_key(item)
            }
        raw_artifact_source_paths = _mapping(artifact_map.get("lineage_source_paths", {}))
        normalized_artifact_source_paths = {
            _path_key(raw_path): paths
            for raw_path, paths in raw_artifact_source_paths.items()
        }
        artifact_source_paths = normalized_artifact_source_paths.get(path)
        if not expected_source_paths and artifact_source_paths:
            expected_source_paths = {
                _path_key(item) for item in _as_list(artifact_source_paths) if _path_key(item)
            }
        self_lineage = lineage.get(path) in ([path], (path,))
        if self_lineage:
            lineage_ids = field_ids
        if expected_field:
            expected_value_matches = "value" not in expected_field or expected_field.get("value") == value
            expected_unit_matches = "unit" not in expected_field or expected_field.get("unit") == field.get("unit")
            expected_availability_matches = (
                "availability" not in expected_field
                or str(expected_field.get("availability")).lower() == availability
            )
            expected_ids_match = not expected_ids or expected_ids == field_ids
        else:
            expected_value_matches = expected_unit_matches = expected_availability_matches = expected_ids_match = True
        single_evidence_value_mismatch = False
        evidence_source_path = _path_key(
            manifest.get(next(iter(field_ids)), {}).get("source_path")
        ) if len(field_ids) == 1 else ""
        if len(field_ids) == 1 and (
            expected_field
            or (not expected_source_paths and evidence_source_path == path)
        ):
            evidence_record = manifest.get(next(iter(field_ids)), {})
            if "value" in evidence_record and evidence_record.get("value") != value:
                single_evidence_value_mismatch = True
        if (
            actual is _MISSING
            or actual != value
            or "path" not in field
            or _path_key(field.get("path")) != path
            or availability not in {"complete", "partial", "unavailable"}
            or single_evidence_value_mismatch
            or path not in lineage
            or (availability == "unavailable" and value is not None)
            or (
                availability != "unavailable"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and not field_ids
            )
            or (lineage_ids != field_ids)
            or not expected_value_matches
            or not expected_unit_matches
            or not expected_availability_matches
            or not expected_ids_match
            or (field_ids and not _valid_evidence_ids(
                field_ids,
                manifest=manifest,
                version_id=version_id,
                result=result,
                expected_paths=expected_source_paths or {path},
                actual_manifest=actual_manifest,
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
            source_paths_by_path=source_paths_by_path,
            actual_manifest=actual_manifest,
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
            source_paths_by_path=source_paths_by_path,
            actual_manifest=actual_manifest,
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
        and (
            _validate_kol_artifact(result, fixture, artifact)[0]
            if artifact.get("artifact_type") == "kol_selection_v3"
            else _validate_structured_artifact(result, fixture, artifact)[0]
        )
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


def _valid_candidates(
    result: Any, fixture: Any, artifact_override: Mapping[str, Any] | None = None
) -> bool:
    if _explicit_false(result, "valid_candidates"):
        return False
    required = _get(fixture, "required_artifact_type", default=None)
    if required != "kol_selection_v3":
        return True
    artifacts = [artifact for artifact in _artifacts(result) if artifact.get("artifact_type") == required]
    if artifact_override is not None:
        artifacts = [dict(artifact_override)]
    if len(artifacts) != 1:
        return False
    artifact = artifacts[0]
    version_id = str(artifact.get("version_id") or "")
    expected_version = _get(fixture, "published_version_id", "expected_version_id", default=None)
    validation_json = artifact.get("validation_json")
    if (
        artifact.get("status") != "published"
        or not isinstance(validation_json, Mapping)
        or validation_json.get("valid") is not True
        or not _nonempty(expected_version)
        or version_id != str(expected_version)
        or version_id not in _declared_artifact_versions(result)
    ):
        return False
    payload = _mapping(artifact.get("payload", artifact.get("payload_json")))
    if payload.get("schema_version") != "kol_selection_v3":
        return False
    data = _mapping(payload.get("data"))
    candidates = _as_list(data.get("items"))
    if not candidates or not all(_valid_candidate(item) for item in candidates):
        return False
    scope = _mapping(payload.get("scope"))
    expected_scope = _mapping(_get(fixture, "scope", "expected_scope", default={}))
    if not _scope_subset(expected_scope, scope):
        return False
    allowed_raw = (
        scope["platforms"] if "platforms" in scope else expected_scope.get("platforms")
    )
    raw_platforms = _as_list(allowed_raw)
    if not raw_platforms or any(
        not isinstance(item, str) or item.strip().casefold() not in _ALLOWED_PLATFORMS
        for item in raw_platforms
    ):
        return False
    allowed = {
        str(item).strip().casefold()
        for item in raw_platforms
    }
    if any(
        str(_mapping(item).get("platform", "")).strip().casefold() not in allowed
        for item in candidates
    ):
        return False
    top_limit = scope["top_limit"] if "top_limit" in scope else expected_scope.get("top_limit")
    if (
        isinstance(top_limit, bool)
        or not isinstance(top_limit, int)
        or top_limit <= 0
        or len(candidates) > top_limit
    ):
        return False
    summary = _mapping(data.get("summary"))
    selected_count = summary.get("selected_count")
    if selected_count != len(candidates):
        return False
    candidate_count = summary.get("candidate_count")
    if candidate_count is not None and candidate_count != len(candidates):
        return False
    declared = _as_list(_get(result, "candidates", "items", default=[]))
    if declared:
        actual_keys = [
            (str(_mapping(item).get("platform", "")), str(_mapping(item).get("kol_uid", "")))
            for item in candidates
        ]
        declared_keys = [
            (str(_mapping(item).get("platform", "")), str(_mapping(item).get("kol_uid", "")))
            for item in declared
        ]
        if declared_keys != actual_keys:
            return False
    identities = [
        str(_mapping(item).get("kol_uid") or _mapping(item).get("stable_id") or "").strip()
        for item in candidates
    ]
    if not all(identities) or len(identities) != len(set(identities)):
        return False

    scoring = _mapping(data.get("scoring"))
    scoring_version = scope.get("scoring_version")
    weights = scoring.get("weights")
    if (
        scoring.get("version") != scoring_version
        or scoring.get("method") != "effect_plus_price_efficiency"
        or scoring.get("missing_value_policy") != "missing_as_zero"
        or not isinstance(weights, Mapping)
        or not weights
        or any(
            isinstance(weight, bool) or not isinstance(weight, (int, float))
            for weight in weights.values()
        )
        or abs(sum(float(weight) for weight in weights.values()) - 70.0) > 1e-6
    ):
        return False
    expected_scoring = _mapping(_get(fixture, "expected_scoring", default={}))
    if not expected_scoring or _canonical_json(scoring) != _canonical_json(expected_scoring):
        return False

    manifest = _evidence_manifest(result, fixture, artifact)
    actual_manifest = _actual_evidence_manifest(result, artifact)
    result_ids = _lineage_evidence_ids(_get(result, "evidence_ids", default=[]))
    manifest_ids = {key for key in manifest if not key.startswith("\x00")}
    expected_paths = {
        _path_key(record.get("source_path"))
        for record in manifest.values()
        if _nonempty(record.get("source_path"))
    }
    if (
        "\x00duplicate_manifest" in manifest
        or "\x00duplicate_actual_manifest" in actual_manifest
        or result_ids != manifest_ids
        or not _valid_evidence_ids(
            result_ids,
            manifest=manifest,
            version_id=version_id,
            result=result,
            expected_paths=expected_paths,
            actual_manifest=actual_manifest,
        )
    ):
        return False

    selected_count = summary.get("selected_count")
    if len(result_ids) != selected_count:
        return False
    for evidence_id in result_ids:
        expected_record = manifest[evidence_id]
        if expected_record.get("value") != selected_count:
            return False
        candidate_ref = expected_record.get("candidate")
        if not isinstance(candidate_ref, Mapping):
            return False
        candidate = next(
            (
                item
                for item in candidates
                if str(_mapping(item).get("kol_uid") or "")
                == str(candidate_ref.get("kol_uid") or "")
            ),
            None,
        )
        if candidate is None:
            return False
        for key in ("kol_uid", "platform", "nickname"):
            if _mapping(candidate).get(key) != candidate_ref.get(key):
                return False
        actual_record = actual_manifest.get(evidence_id, {})
        actual_candidate = _mapping(actual_record.get("candidate"))
        if any(actual_candidate.get(key) != candidate_ref.get(key) for key in ("kol_uid", "platform", "nickname")):
            return False
        snapshot = _mapping(_mapping(candidate).get("score_snapshot"))
        if snapshot.get("version") != scope.get("scoring_version"):
            return False
        effect_score = snapshot.get("effect_score")
        price_score = snapshot.get("price_efficiency_score")
        value_score = snapshot.get("value_score")
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in (effect_score, price_score, value_score)
        ) or abs(float(effect_score) + float(price_score) - float(value_score)) > 1e-6:
            return False
        dimensions = snapshot.get("dimensions")
        if not isinstance(dimensions, Mapping) or not dimensions or set(dimensions) != set(weights):
            return False
        if any(
            not _nonempty(_get(dimension, "source", default=None))
            or not isinstance(_get(dimension, "raw_score", default=None), (int, float))
            or not isinstance(_get(dimension, "weighted_score", default=None), (int, float))
            or _get(dimension, "weight", default=None) != weights.get(name)
            for name, dimension in dimensions.items()
        ):
            return False
        weighted_total = sum(
            float(_get(dimension, "weighted_score", default=0))
            for dimension in dimensions.values()
        )
        if abs(weighted_total - float(effect_score)) > 1e-6:
            return False
    lineage_paths = _mapping(artifact.get("lineage_source_paths", {}))
    if not lineage_paths:
        return False
    source_candidate = _mapping(
        _mapping(actual_manifest.get(next(iter(result_ids)), {})).get("candidate")
    )
    for path, sources in lineage_paths.items():
        if _resolve_payload_path(payload, path) is _MISSING:
            return False
        source_list = _as_list(sources)
        if not source_list or any(
            not isinstance(source, str) or not source.strip() for source in source_list
        ):
            return False
        for source in source_list:
            source_key = _path_key(source)
            if not source_key.startswith("0/") or _resolve_payload_path(
                source_candidate, source_key.removeprefix("0/")
            ) is _MISSING:
                return False
    selected_sources = _as_list(lineage_paths.get("/data/summary/selected_count"))
    if not selected_sources or any(
        not isinstance(source, str) or not source.startswith("/0/")
        for source in selected_sources
    ):
        return False
    expected_lineage = _mapping(_get(fixture, "expected_lineage_source_paths", default={}))
    for path, expected_sources in expected_lineage.items():
        actual = {
            source for source in _as_list(lineage_paths.get(path)) if isinstance(source, str)
        }
        expected = {
            source for source in _as_list(expected_sources) if isinstance(source, str)
        }
        if actual != expected:
            return False
    return True


def _validate_kol_artifact(
    result: Any, fixture: Any, artifact: Mapping[str, Any]
) -> tuple[bool, bool, bool]:
    """验证 KOL v3 的候选、叙事与受限披露，不读取自报布尔值。"""
    payload = _mapping(artifact.get("payload", artifact.get("payload_json")))
    availability = _mapping(payload.get("availability"))
    if set(availability) != _KOL_REQUIRED_AVAILABILITY_SECTIONS or any(
        not isinstance(section, Mapping)
        or str(section.get("status", "")).lower() not in _KOL_AVAILABILITY_STATUSES
        for section in availability.values()
    ):
        return False, False, False
    if not _valid_candidates(result, fixture, artifact):
        return False, False, False
    narrative = _mapping(payload.get("narrative"))
    narrative_ok = _nonempty(narrative.get("selection_summary")) and bool(
        _as_list(narrative.get("fit_findings"))
    )
    for key in ("fit_findings", "risk_notes", "usage_advice"):
        entries = _as_list(narrative.get(key))
        if not all(
            _nonempty(_get(entry, "text", default=None))
            and bool(_as_list(_get(entry, "supporting_paths", default=[])))
            and all(
                _resolve_narrative_path(payload, path) is not _MISSING
                for path in _as_list(_get(entry, "supporting_paths", default=[]))
            )
            for entry in entries
        ):
            narrative_ok = False
    limitations = _as_list(artifact.get("limitations", payload.get("limitations", [])))
    limitation_ok = all(
        bool(_as_list(_get(item, "affected_paths", default=[]))) for item in limitations
    )
    limitation_paths = {
        _normalize_section_path(path)
        for item in limitations
        for path in _as_list(_get(item, "affected_paths", default=[]))
        if _normalize_section_path(path)
    }
    has_partial = any(
        str(_get(section, "status", default="")).lower() in {"partial", "unavailable"}
        for section in availability.values()
    )
    for section, state in availability.items():
        if str(_get(state, "status", default="")).lower() not in {"partial", "unavailable"}:
            continue
        section_path = _normalize_section_path(section)
        if (
            _resolve_payload_path(payload, f"/{section_path}") is _MISSING
            or not any(
                path == section_path or path.startswith(f"{section_path}/")
                for path in limitation_paths
            )
        ):
            limitation_ok = False
    return True, narrative_ok, limitation_ok and (not has_partial or bool(limitations))


def _narrative_grounded(result: Any, fixture: Any) -> bool:
    if _behavior(result, fixture) != _REPORT:
        return True
    artifacts = _artifacts(result)
    if not _report_has_expected_artifact(result, fixture):
        return False
    return all(
        not _artifact_self_reported_false(artifact, "narrative_grounded")
        and (
            _validate_kol_artifact(result, fixture, artifact)[1]
            if artifact.get("artifact_type") == "kol_selection_v3"
            else _validate_structured_artifact(result, fixture, artifact)[1]
        )
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
    if _datatap_calls(result) != 0:
        return False
    source = _mapping(_get(result, "source_version", default={}))
    source_artifact = _mapping(_get(result, "source_artifact", default={}))
    version_id = str(source.get("version_id") or "")
    expected_version = str(_get(fixture, "published_version_id", "expected_version_id", default="") or "")
    artifact_type = str(source.get("artifact_type") or "")
    payload = source.get("payload", source.get("payload_json"))
    expected_digest = str(
        _get(fixture, "published_payload_digest", "source_payload_digest", default="") or ""
    )
    if (
        version_id != expected_version
        or not artifact_type
        or not isinstance(payload, Mapping)
        or not expected_digest
        or source_artifact.get("status") != "published"
        or not isinstance(source_artifact.get("validation_json"), Mapping)
        or source_artifact["validation_json"].get("valid") is not True
        or str(source_artifact.get("version_id") or "") != version_id
        or str(source_artifact.get("artifact_type") or "") != artifact_type
        or _canonical_json(source_artifact.get("payload")) != _canonical_json(payload)
    ):
        return False
    payload_digest = source.get("payload_digest")
    if (
        not isinstance(payload_digest, str)
        or payload_digest != _digest_text(payload)
        or payload_digest != expected_digest
    ):
        return False
    records = _as_list(_get(result, "read_records", default=[]))
    single_record = _get(result, "read_record", default=None)
    if single_record is not None:
        records.append(single_record)
    if len(records) != 1:
        return False
    record = _mapping(records[0])
    if (
        str(record.get("version_id") or "") != version_id
        or str(record.get("artifact_type") or "") != artifact_type
        or record.get("payload_digest") != payload_digest
    ):
        return False
    path = record.get("path")
    value = _resolve_payload_path(payload, path)
    if value is _MISSING or value != record.get("value"):
        return False
    claims = _as_list(_get(result, "claims", "answer_claims", default=[]))
    if not claims:
        return False
    for claim in claims:
        item = _mapping(claim)
        claim_path = _path_key(item.get("path"))
        supporting = {_path_key(path_value) for path_value in _as_list(item.get("supporting_paths"))}
        if claim_path != _path_key(path) or claim_path not in supporting or item.get("value") != value:
            return False
    return True


def _digest_text(value: Any) -> str:
    import hashlib

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


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
    declared_versions, artifact_versions = _raw_artifact_version_ids(result)
    if len(declared_versions) != len(set(declared_versions)):
        return False
    if len(artifact_versions) != len(set(artifact_versions)):
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
        and (
            _validate_kol_artifact(result, fixture, artifact)[2]
            if artifact.get("artifact_type") == "kol_selection_v3"
            else _validate_structured_artifact(result, fixture, artifact)[2]
        )
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
