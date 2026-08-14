"""B0 离线回放使用的生产纯值对象管线。

该模块只编排既有 Capability Pack、确定性 Builder、发布校验和正式 Exporter；
不创建数据库、模型、MCP 或钱包客户端。Pi replay 只负责读取 fixture 与驱动事件。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any

from app.agent_artifacts import exporters as artifact_exporters
from app.agent_artifacts import publication_core
from app.agent_artifacts.builders import brand as brand_builder
from app.agent_artifacts.builders import campaign as campaign_builder
from app.agent_artifacts.builders import kol_selection as kol_builder
from app.agent_artifacts.lineage import EvidenceScope
from app.agent_runtime.tools.contracts import ToolContext


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_export_bytes(exported: bytes) -> bytes:
    """去除 XLSX core.xml 的当前时间，保留正式导出内容并生成稳定摘要。"""
    try:
        source = zipfile.ZipFile(io.BytesIO(exported))
    except (zipfile.BadZipFile, TypeError):
        return exported
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "docProps/core.xml":
                data = re.sub(
                    rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                    rb"\g<1>2000-01-01T00:00:00Z\g<2>",
                    data,
                )
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            target.writestr(info, data)
    return output.getvalue()


def _event(sequence: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    matches = [event for event in sequence if event.get("type") == event_type]
    if len(matches) != 1:
        raise ValueError("offline_replay_event_duplicate")
    return matches[0]


def _json_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if not isinstance(value, dict):
        raise TypeError("offline_replay_builder_output_invalid")
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _json_default(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _stabilize_offline_payload(payload: dict[str, Any]) -> None:
    """固定 Builder 的生成时间，确保同一 synthetic fixture 可重复回放。"""
    methodology = payload.get("methodology")
    if isinstance(methodology, dict) and "data_as_of" in methodology:
        methodology["data_as_of"] = "2026-01-31T23:59:59Z"


def _period() -> dict[str, str]:
    return {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"}


def _scope_for_builder(case: dict[str, Any]) -> dict[str, Any]:
    scope = dict(case.get("scope") or {})
    scope.setdefault("period", _period())
    if case.get("required_artifact_type") in {"brand_report_v3", "campaign_report_v2"}:
        scope.setdefault("keywords", [])
        scope.setdefault("comparison_mode", "none")
    if case.get("required_artifact_type") == "campaign_report_v2":
        scope.setdefault("campaign", "脱敏活动")
    if case.get("required_artifact_type") == "kol_selection_v3":
        scope.setdefault("category", "美妆")
        scope.setdefault(
            "audience",
            {"regions": ["全国"], "age_ranges": ["18-35"], "interests": ["美妆"]},
        )
        scope.setdefault("region", ["全国"])
        scope.setdefault("age_range", ["18-35"])
        scope.setdefault("budget", {"min": 1000, "max": 100000})
        scope.setdefault(
            "filters", {"budget_min": 1000, "budget_max": 100000}
        )
        scope.setdefault("ranking_mode", "balanced")
        scope.setdefault("top_limit", 20)
        scope.setdefault("scoring_version", "kol_value_score_v3")
    return scope


def _builder_evidence(
    case: dict[str, Any], records: dict[str, dict[str, Any]]
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    artifact_type = case.get("required_artifact_type")
    if artifact_type == "brand_report_v3":
        return {
            "overview_current": [
                (
                    evidence_id,
                    {"platform": "douyin", "volume": record.get("value")},
                )
                for evidence_id, record in records.items()
            ]
        }
    if artifact_type == "campaign_report_v2":
        rows: list[tuple[str, dict[str, Any]]] = []
        for evidence_id, record in records.items():
            row = {"platform": "douyin", "post_id": evidence_id}
            if record.get("unit") == "interactions" or "engagement" in str(
                record.get("source_path")
            ):
                row["engagement"] = record.get("value")
            else:
                row["volume"] = record.get("value")
            rows.append((evidence_id, row))
        return {"posts": rows}
    return {}


def _build_kol(case: dict[str, Any], records: dict[str, dict[str, Any]]) -> Any:
    scope = _scope_for_builder(case)
    items: list[dict[str, Any]] = []
    for record in records.values():
        candidate = record.get("candidate")
        if not isinstance(candidate, dict):
            raise TypeError("offline_replay_candidate_missing")
        items.append(
            {
                **candidate,
                "score_inputs": {
                    "followers": 10000,
                    "average_interactions": 100,
                    "active_follower_rate": 50,
                    "interaction_follower_ratio": 1,
                    "content_score": 80,
                    "industry_interest": 80,
                    "target_region": 80,
                    "target_age": 80,
                },
                "quoted_price": candidate.get("quoted_price", 10000),
            }
        )
    context = ToolContext(
        user_id="offline-user",
        session_id="offline-session",
        run_id="offline-run",
        profile_name="offline-replay",
    )
    return asyncio.run(
        kol_builder.build_kol_selection_draft(
            scope=scope,
            evidence_id=next(iter(records)),
            items=items,
            context=context,
            db=None,
            source_names=("offline_synthetic_evidence",),
        )
    )


def _build_draft(
    case: dict[str, Any], records: dict[str, dict[str, Any]]
) -> Any:
    artifact_type = case.get("required_artifact_type")
    if artifact_type == "brand_report_v3":
        return brand_builder.build_brand_report_draft(
            scope=_scope_for_builder(case),
            evidence=_builder_evidence(case, records),
            source_names=("offline_synthetic_evidence",),
        )
    if artifact_type == "campaign_report_v2":
        return campaign_builder.build_campaign_report_draft(
            scope=_scope_for_builder(case),
            evidence=_builder_evidence(case, records),
            source_names=("offline_synthetic_evidence",),
        )
    if artifact_type == "kol_selection_v3":
        return _build_kol(case, records)
    raise ValueError("offline_replay_builder_input_invalid")


def _scope_for_validation(
    records: dict[str, dict[str, Any]], refs: list[dict[str, Any]], version_id: str
) -> EvidenceScope:
    field_ids: dict[str, set[str]] = {}
    for ref in refs:
        path = ref.get("artifact_path")
        if not isinstance(path, str):
            continue
        field_ids[path] = {
            str(source.get("evidence_id"))
            for source in ref.get("sources", [])
            if isinstance(source, dict) and source.get("evidence_id")
        }
    return EvidenceScope(
        session_id=next(iter(records.values())).get("session_id") if records else None,
        run_id=next(iter(records.values())).get("run_id") if records else None,
        evidence=records,
        allowed_artifact_version_ids=frozenset({version_id}),
        field_evidence_ids={path: frozenset(ids) for path, ids in field_ids.items()},
    )


def _source_paths_by_field(refs: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for ref in refs:
        path = ref.get("artifact_path")
        if not isinstance(path, str):
            continue
        result[path] = [
            str(source.get("source_path"))
            for source in ref.get("sources", [])
            if isinstance(source, dict) and source.get("source_path")
        ]
    return result


def _lineage_artifact(
    *,
    draft_payload: dict[str, Any],
    draft_refs: list[dict[str, Any]],
    version_id: str,
    artifact_type: str,
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    canonical = draft_payload.get("canonical_data")
    fields = [
        item
        for item in (canonical if isinstance(canonical, list) else ())
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]
    field_lineage: dict[str, dict[str, Any]] = {}
    source_paths = _source_paths_by_field(draft_refs)
    claims: list[dict[str, Any]] = []
    for field in fields:
        path = field["path"]
        ids = list(field.get("evidence_ids") or [])
        field_lineage[path] = {"evidence_ids": ids}
        value = field.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value is not None:
            claim = {
                "path": path,
                "value": value,
                "supporting_paths": [path],
                "evidence_ids": ids,
            }
            claims.append(claim)
    limitations = list(draft_payload.get("limitations") or [])
    for field in fields:
        if field.get("availability") in {"partial", "unavailable"}:
            limitations.append({"affected_paths": [field["path"]], "code": "offline_gap"})
    return {
        "version_id": version_id,
        "artifact_type": artifact_type,
        "status": "draft",
        "validation_json": {"valid": False},
        "payload": draft_payload,
        "canonical_data": fields,
        "field_lineage": field_lineage,
        "lineage_source_paths": source_paths,
        "structured_claims": claims,
        "narrative_claims": list(claims),
        "availability": {
            field["path"]: field.get("availability") for field in fields
        },
        "limitations": limitations,
        "evidence_manifest": list(records.values()),
    }


@dataclass(frozen=True)
class BuiltArtifact:
    artifact: dict[str, Any]
    draft_payload: dict[str, Any]
    draft_refs: list[dict[str, Any]]


@dataclass(frozen=True)
class _OfflineVersion:
    """回放导出使用的只读 Version 视图。"""

    schema_version: str
    payload_json: dict[str, Any]
    status: str
    validation_json: dict[str, Any]


def build_artifact(
    *,
    case: dict[str, Any],
    sequence: list[dict[str, Any]],
    records: dict[str, dict[str, Any]],
    capability: Any,
) -> BuiltArtifact:
    build_event = _event(sequence, "artifact.build")
    version_id = str(build_event.get("version_id") or "")
    artifact_type = str(build_event.get("artifact_type") or "")
    if artifact_type != case.get("required_artifact_type") or not version_id:
        raise ValueError("offline_replay_builder_input_invalid")
    draft = _build_draft(case, records)
    draft_payload = _json_payload(draft.payload)
    _stabilize_offline_payload(draft_payload)
    # Capability snapshot is an explicit input to the run; contract availability
    # is checked before any payload is accepted.
    contracts = {str(item.get("artifact_type")) for item in capability.artifact_contracts}
    contract_type = "campaign_report_v3" if artifact_type == "campaign_report_v2" else artifact_type
    if contract_type not in contracts:
        raise ValueError("offline_replay_capability_contract_missing")
    scope = _scope_for_validation(records, draft.evidence_refs, version_id)
    normalized, errors = publication_core.validate_payload_for_publication(
        module=draft.module,
        schema_version=draft.schema_version,
        artifact_type=draft.artifact_type,
        payload=draft_payload,
        evidence_scope=scope,
        artifact_version_id=version_id,
        enforce_kol_publication_validity=artifact_type == "kol_selection_v3",
    )
    if errors or normalized is None:
        raise ValueError("offline_replay_validator_failed")
    artifact = _lineage_artifact(
        draft_payload=normalized,
        draft_refs=draft.evidence_refs,
        version_id=version_id,
        artifact_type=artifact_type,
        records=records,
    )
    expected_path = str(build_event.get("path") or "").removeprefix("/")
    expected_value = build_event.get("value")
    actual = normalized
    for part in expected_path.split("/") if expected_path else ():
        if not isinstance(actual, dict) or part not in actual:
            raise ValueError("offline_replay_builder_path_invalid")
        actual = actual[part]
    if expected_path and actual != expected_value:
        raise ValueError("offline_replay_builder_value_mismatch")
    narrative_event = _event(sequence, "narrative.claim")
    if (
        str(narrative_event.get("path") or "").removeprefix("/") != expected_path
        or narrative_event.get("value") != expected_value
    ):
        raise ValueError("offline_replay_narrative_input_invalid")
    return BuiltArtifact(artifact, normalized, list(draft.evidence_refs))


def publish_and_export(
    *,
    built: BuiltArtifact,
    publish_event: dict[str, Any],
    export_event: dict[str, Any],
) -> bytes:
    artifact = built.artifact
    if (
        publish_event.get("version_id") != artifact["version_id"]
        or publish_event.get("artifact_type") != artifact["artifact_type"]
    ):
        raise ValueError("offline_replay_publication_version_invalid")
    artifact["status"] = "published"
    artifact["validation_json"] = {"valid": True}
    version = _OfflineVersion(
        schema_version=artifact["artifact_type"],
        payload_json=artifact["payload"],
        status="published",
        validation_json={"valid": True},
    )
    expected_format = export_event.get("format")
    if expected_format != "xlsx":
        raise ValueError("offline_replay_export_format_invalid")
    if (
        export_event.get("version_id") != artifact["version_id"]
        or export_event.get("artifact_type") != artifact["artifact_type"]
    ):
        raise ValueError("offline_replay_export_artifact_invalid")
    exported = artifact_exporters.export_artifact(version)
    if not exported:
        raise ValueError("offline_replay_export_empty")
    artifact["exported"] = True
    artifact["export_digest"] = hashlib.sha256(_canonical_export_bytes(exported)).hexdigest()
    return exported


__all__ = [
    "BuiltArtifact",
    "build_artifact",
    "canonical_json",
    "payload_digest",
    "publish_and_export",
]
