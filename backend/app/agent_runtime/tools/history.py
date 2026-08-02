"""历史读取工具（设计文档 §九 / §10.2「大结果处理」）。

三个 TrustedTool（HISTORY_TOOLS 分类）：
- ``read_artifact(artifact_id, version?, section?)``：读取已发布 Artifact
  的 payload（或某个 section）；
- ``search_evidence(query, artifact_id?, run_id?, filters?)``：按当前用户 +
  Session 范围搜索 evidence_items；
- ``read_tool_result(evidence_id, cursor?, limit?)``：按游标分片读取 Evidence
  原始结果，绝不整页返回。

每个工具都校验 Evidence/Artifact 属于当前用户和 Session：缺失返回
``not_found``，跨用户返回 ``forbidden``（Router 层统一映射为 404）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.models import AgentRun, AgentSession, EvidenceItem
from app.agent_runtime.tools.contracts import ToolContext, ToolResult

# 结构化错误类型；Router 层把两者都映射为 404（§九「跨 Session 返回 404」）。
NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"

# search_evidence 默认返回的匹配上限（无分页参数，超出置 truncated）。
_SEARCH_MATCH_LIMIT = 20
# read_tool_result 默认页大小。
_TOOL_RESULT_DEFAULT_LIMIT = 20
# search_evidence filters 支持的等值列。
_FILTER_COLUMNS: dict[str, Any] = {
    "source_type": EvidenceItem.source_type,
    "source_name": EvidenceItem.source_name,
    "availability_status": EvidenceItem.availability_status,
}


def _failed(error_type: str, message: str) -> ToolResult:
    return ToolResult(status="failed", safe_summary=message, error_type=error_type)


async def _owned_session(
    db: AsyncSession, context: ToolContext
) -> tuple[AgentSession | None, str | None]:
    """校验 Session 属于当前用户；返回 (session, None) 或 (None, error_type)。"""
    session = await db.get(AgentSession, context.session_id)
    if session is None:
        return None, NOT_FOUND
    if session.user_id != context.user_id:
        return None, FORBIDDEN
    return session, None


def _dig(payload: Any, path: str) -> Any:
    """按点分路径钻取 payload；路径不存在返回 None。"""
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


# --------------------------------------------------------------------------- #
# read_artifact
# --------------------------------------------------------------------------- #


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(min_length=1)
    version: int | None = None
    section: str | None = None


class ReadArtifactTool:
    """读取已发布 Artifact payload 或其 section（只读、零积分）。"""

    name = "read_artifact"
    input_model = ReadArtifactArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = ReadArtifactArgs.model_validate(arguments)
        _session, error = await _owned_session(self._db, context)
        if error is not None:
            return _failed(error, "session_" + error)

        artifact = await self._db.get(AgentArtifact, args.artifact_id)
        if artifact is None or artifact.session_id != context.session_id:
            return _failed(NOT_FOUND, "artifact_not_found")
        if artifact.user_id != context.user_id:
            return _failed(FORBIDDEN, "artifact_forbidden")

        version = args.version if args.version is not None else artifact.latest_version
        version_row = await self._db.scalar(
            select(AgentArtifactVersion).where(
                AgentArtifactVersion.artifact_id == artifact.id,
                AgentArtifactVersion.version == version,
            )
        )
        if version_row is None:
            return _failed(NOT_FOUND, "artifact_version_not_found")

        payload = version_row.payload_json if version_row.payload_json is not None else {}
        section: str | None = None
        if args.section:
            section = args.section
            payload = _dig(payload, args.section)
            if payload is None:
                return _failed(NOT_FOUND, "artifact_section_not_found")

        summary = json.dumps(
            {
                "artifact_id": artifact.id,
                "version": version,
                "section": section,
                "payload": payload,
            },
            ensure_ascii=False,
        )
        return ToolResult(status="success", safe_summary=summary)


# --------------------------------------------------------------------------- #
# search_evidence
# --------------------------------------------------------------------------- #


class SearchEvidenceArgs(BaseModel):
    query: str = ""
    artifact_id: str | None = None
    run_id: str | None = None
    filters: dict[str, Any] | None = None


class SearchEvidenceTool:
    """按当前用户 + Session 范围搜索 evidence_items（只读、零积分）。"""

    name = "search_evidence"
    input_model = SearchEvidenceArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = SearchEvidenceArgs.model_validate(arguments)
        _session, error = await _owned_session(self._db, context)
        if error is not None:
            return _failed(error, "session_" + error)

        conditions = [EvidenceItem.session_id == context.session_id]
        if args.run_id:
            run = await self._db.get(AgentRun, args.run_id)
            if run is None or run.session_id != context.session_id:
                return _failed(NOT_FOUND, "run_not_found")
            if run.user_id != context.user_id:
                return _failed(FORBIDDEN, "run_forbidden")
            conditions.append(EvidenceItem.run_id == args.run_id)
        if args.artifact_id:
            artifact = await self._db.get(AgentArtifact, args.artifact_id)
            if artifact is None or artifact.session_id != context.session_id:
                return _failed(NOT_FOUND, "artifact_not_found")
            if artifact.user_id != context.user_id:
                return _failed(FORBIDDEN, "artifact_forbidden")
            referenced = await self._evidence_ids_for_artifact(artifact)
            conditions.append(EvidenceItem.id.in_(referenced) if referenced else EvidenceItem.id.in_([""]))
        if args.filters:
            for key, value in args.filters.items():
                column = _FILTER_COLUMNS.get(key)
                if column is not None:
                    conditions.append(column == value)

        rows = (
            await self._db.scalars(
                select(EvidenceItem)
                .where(*conditions)
                .order_by(EvidenceItem.collected_at.desc(), EvidenceItem.id.desc())
            )
        ).all()
        matches = [item for item in rows if self._matches(item, args.query or "")]
        total = len(matches)
        page = matches[:_SEARCH_MATCH_LIMIT]
        summary = json.dumps(
            {
                "query": args.query,
                "total_matches": total,
                "truncated": total > _SEARCH_MATCH_LIMIT,
                "matches": [
                    {
                        "evidence_id": item.id,
                        "source_type": item.source_type,
                        "source_name": item.source_name,
                        "run_id": item.run_id,
                        "collected_at": item.collected_at.isoformat() if item.collected_at else None,
                        "preview": item.normalized_preview_json,
                    }
                    for item in page
                ],
            },
            ensure_ascii=False,
        )
        return ToolResult(status="success", safe_summary=summary)

    @staticmethod
    def _matches(item: EvidenceItem, query: str) -> bool:
        if not query:
            return True
        blob = " ".join(
            part
            for part in (
                item.source_name or "",
                item.source_type or "",
                json.dumps(item.scope_json, ensure_ascii=False) if item.scope_json else "",
                json.dumps(item.period_json, ensure_ascii=False) if item.period_json else "",
                json.dumps(item.normalized_preview_json, ensure_ascii=False)
                if item.normalized_preview_json
                else "",
            )
            if part
        )
        return query.casefold() in blob.casefold()

    async def _evidence_ids_for_artifact(self, artifact: AgentArtifact) -> list[str]:
        """返回该 Artifact 最新已发布版本 evidence_refs 引用的 evidence_ids。"""
        version_row = await self._db.scalar(
            select(AgentArtifactVersion)
            .where(AgentArtifactVersion.artifact_id == artifact.id)
            .order_by(AgentArtifactVersion.version.desc())
        )
        if version_row is None:
            return []
        ids: set[str] = set()
        for ref in version_row.evidence_refs_json or []:
            if not isinstance(ref, dict):
                continue
            for source in ref.get("sources") or []:
                if (
                    isinstance(source, dict)
                    and source.get("source_type") == "evidence"
                    and isinstance(source.get("evidence_id"), str)
                    and source["evidence_id"]
                ):
                    ids.add(source["evidence_id"])
        return sorted(ids)


# --------------------------------------------------------------------------- #
# read_tool_result
# --------------------------------------------------------------------------- #


class ReadToolResultArgs(BaseModel):
    evidence_id: str = Field(min_length=1)
    cursor: int | None = None
    limit: int = _TOOL_RESULT_DEFAULT_LIMIT


class ReadToolResultTool:
    """按游标分片读取 Evidence 原始结果；大结果绝不整页返回（§10.2）。"""

    name = "read_tool_result"
    input_model = ReadToolResultArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = ReadToolResultArgs.model_validate(arguments)
        _session, error = await _owned_session(self._db, context)
        if error is not None:
            return _failed(error, "session_" + error)

        evidence = await self._db.get(EvidenceItem, args.evidence_id)
        if evidence is None or evidence.session_id != context.session_id:
            # 不泄露跨 Session evidence 的存在性。
            return _failed(NOT_FOUND, "evidence_not_found")

        sequence, total = self._sequence(evidence.raw_payload_json)
        offset = max(args.cursor or 0, 0)
        limit = max(args.limit, 1)
        page = sequence[offset : offset + limit]
        next_offset = offset + limit
        next_cursor = str(next_offset) if next_offset < total else None
        truncated = next_cursor is not None
        summary = json.dumps(
            {
                "evidence_id": evidence.id,
                "items": page,
                "total": total,
                "next_cursor": next_cursor,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )
        return ToolResult(
            status="success",
            safe_summary=summary,
            cursor=next_cursor,
            truncated=truncated,
        )

    @staticmethod
    def _sequence(raw: Any) -> tuple[list[Any], int]:
        """把原始结果归一到可分片的序列。"""
        if isinstance(raw, list):
            return raw, len(raw)
        if isinstance(raw, dict) and isinstance(raw.get("rows"), list):
            rows = raw["rows"]
            return rows, len(rows)
        return [raw], 1


__all__ = [
    "FORBIDDEN",
    "NOT_FOUND",
    "ReadArtifactArgs",
    "ReadArtifactTool",
    "ReadToolResultArgs",
    "ReadToolResultTool",
    "SearchEvidenceArgs",
    "SearchEvidenceTool",
]
