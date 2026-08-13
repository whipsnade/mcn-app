"""发布链路共享的纯值对象校验。

数据库发布服务和离线回放都只能通过这里组合 payload 强类型校验与结构化
lineage 校验；本模块不持有数据库、配置或外部客户端。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.agent_artifacts.lineage import EvidenceScope, validate_structured_claims
from app.agent_artifacts.validation import ArtifactPayloadValidator


def validate_payload_for_publication(
    *,
    module: str,
    schema_version: str,
    artifact_type: str,
    payload: Any,
    evidence_scope: EvidenceScope | Mapping[str, Any] | None = None,
    artifact_version_id: str | None = None,
    enforce_kol_publication_validity: bool = True,
    direct_model_payload: bool = False,
    structured_validator: Callable[..., list[Any]] = validate_structured_claims,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """返回标准化 payload 与结构化错误，供 direct/batch/replay 共用。"""

    normalized, errors = ArtifactPayloadValidator.validate_revision_payload_collecting(
        module=module,
        schema_version=schema_version,
        artifact_type=artifact_type,
        payload=payload,
        enforce_kol_publication_validity=enforce_kol_publication_validity,
        direct_model_payload=direct_model_payload,
    )
    if errors or normalized is None or evidence_scope is None or artifact_version_id is None:
        return normalized, list(errors)
    if normalized.get("canonical_data") or normalized.get("field_lineage"):
        structured = structured_validator(
            normalized, artifact_version_id, evidence_scope
        )
        errors.extend(
            {"stage": "structured_claims", **issue.as_dict()} for issue in structured
        )
    return normalized, errors


__all__ = ["validate_payload_for_publication"]
