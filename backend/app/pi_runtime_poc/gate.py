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
    versions = _artifact_versions(result)
    if not versions:
        return False
    if required is None:
        return True
    artifacts = _artifacts(result)
    if not artifacts:
        explicit = _get(result, "expected_artifact", default=None)
        legacy = _get(_get(result, "hard_checks", default={}), "expected_artifact", default=None)
        return explicit is True or legacy is True
    return any(artifact.get("artifact_type") == required for artifact in artifacts)


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


def _numeric_lineage(result: Any, fixture: Any) -> bool:
    if _explicit_false(result, "numeric_lineage_complete"):
        return False
    if _behavior(result, fixture) != _REPORT:
        return True
    if not _report_has_expected_artifact(result, fixture):
        return False
    if not _evidence_ids(result):
        return False
    signal = _signal_from_artifacts(result, "numeric_lineage_complete")
    if signal is not None:
        return signal
    explicit = _explicit_value(result, "numeric_lineage_complete")
    if explicit is not None:
        return explicit
    return _explicit_value(result, "lineage_complete") is True


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
    if not candidates or not all(_valid_candidate(item) for item in candidates):
        return False
    signal = _signal_from_artifacts(result, "valid_candidates")
    return signal is not False


def _narrative_grounded(result: Any, fixture: Any) -> bool:
    if _explicit_false(result, "narrative_grounded"):
        return False
    if _behavior(result, fixture) != _REPORT:
        return True
    signal = _signal_from_artifacts(result, "narrative_grounded")
    if signal is not None:
        return signal
    explicit = _explicit_value(result, "narrative_grounded")
    if explicit is not None:
        return explicit
    return False


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
    if _explicit_false(result, "partial_limitations_complete"):
        return False
    if _behavior(result, fixture) != _REPORT:
        return True
    signal = _signal_from_artifacts(result, "partial_limitations_complete")
    if signal is False:
        return False
    artifacts = _artifacts(result)
    partial = any(str(artifact.get("availability", "")).lower() == "partial" for artifact in artifacts)
    if not partial:
        explicit = _explicit_value(result, "partial_limitations_complete")
        return explicit is not False and (signal is not None or explicit is not None or bool(artifacts))
    limitations = _get(result, "limitations", "limitation", default=None)
    if not _nonempty(limitations):
        limitations = [artifact.get("limitations") for artifact in artifacts]
    return _has_content(limitations)


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


__all__ = ["HARD_CHECKS", "Summary", "evaluate_case", "finalize_execution", "write_summary_append_once"]
