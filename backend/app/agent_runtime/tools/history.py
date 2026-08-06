"""历史读取工具（设计文档 §九 / §10.2「大结果处理」）与范围记忆工具。

三个只读 TrustedTool（HISTORY_TOOLS 分类）：
- ``read_artifact(artifact_id, version?, section?)``：读取 Artifact 的
  payload（或某个 section）——默认读最新已发布 Version（``status="published"``）；
  Artifact 有活动 Draft（drafting/reviewing，如 Builder 刚产出待审核）时读
  当前 Draft Revision（``status="draft"`` + revision 号），显式 ``version``
  恒读已发布 Version；
- ``search_evidence(query, artifact_id?, run_id?, filters?)``：按当前用户 +
  Session 范围搜索 evidence_items；
- ``read_tool_result(evidence_id, cursor?, limit?)``：按游标分片读取 Evidence
  原始结果，绝不整页返回。

写入侧仅有 ``remember_scope``（同为 HISTORY_TOOLS）：把用户已确认的范围条件
持久化为 ``confirmed_scope`` 记忆条目，同 domain+field 的旧 active 条目被
supersede；Context Builder 只注入未 supersede 条目。

每个工具都校验 Evidence/Artifact/Session 属于当前用户和 Session：缺失返回
``not_found``，跨用户返回 ``forbidden``（Router 层统一映射为 404）。
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypeAliasType

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
)
from app.agent_runtime.evidence import build_model_evidence_view
from app.agent_runtime.models import (
    AgentMessage,
    AgentSession,
    EvidenceItem,
    MemoryEntry,
)
from app.agent_runtime.repository import utc_now
from app.agent_runtime.tools.contracts import ToolContext, ToolResult

# 与 mcp_gateway.transport.JsonValue 同义，但用 TypeAliasType 声明：py311 下
# pydantic 无法为 typing.TypeAlias 的隐式递归别名生成 Schema（RecursionError）。
JsonValue = TypeAliasType(
    "JsonValue",
    "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]",
)

# 结构化错误类型；Router 层把两者都映射为 404（§九「跨 Session 返回 404」）。
NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"

# search_evidence 默认返回的匹配上限（无分页参数，超出置 truncated）。
_SEARCH_MATCH_LIMIT = 20
# search_evidence 单次扫描的 SQL LIMIT（投影后仍避免无限加载整 Session 证据）。
_SEARCH_SCAN_LIMIT = 500
# read_tool_result 默认页大小与页大小上限（§10.2：绝不整页返回大结果）。
_TOOL_RESULT_DEFAULT_LIMIT = 20
_TOOL_RESULT_MAX_LIMIT = 200
# search_evidence filters 支持的等值列。
_FILTER_COLUMNS: dict[str, Any] = {
    "source_type": EvidenceItem.source_type,
    "source_name": EvidenceItem.source_name,
    "availability_status": EvidenceItem.availability_status,
}
# search_evidence 只投影匹配/展示所需列，绝不加载大字段 raw_payload_json。
_EVIDENCE_MATCH_COLUMNS = (
    EvidenceItem.id,
    EvidenceItem.run_id,
    EvidenceItem.source_type,
    EvidenceItem.source_name,
    EvidenceItem.scope_json,
    EvidenceItem.period_json,
    EvidenceItem.normalized_preview_json,
    EvidenceItem.collected_at,
)
# 参数校验失败的结构化错误类型。
INVALID_ARGUMENTS = "invalid_arguments"


def _failed(error_type: str, message: str) -> ToolResult:
    return ToolResult(status="failed", safe_summary=message, error_type=error_type)


def _parse_args(
    model_cls: type[BaseModel], arguments: Any
) -> tuple[BaseModel | None, ToolResult | None]:
    """校验工具参数；失败返回结构化错误而非抛异常。"""
    try:
        return model_cls.model_validate(arguments), None
    except ValidationError as exc:
        return None, _failed(INVALID_ARGUMENTS, f"invalid arguments: {exc}")


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


# Draft section 切片缺失哨兵（None 是合法 payload 值，不能当缺失用）。
_MISSING = object()

# 活动 Draft 状态：发布收尾后 release_draft 复位为 idle/failed，不再视为活动。
_ACTIVE_DRAFT_STATUSES = frozenset({"drafting", "reviewing"})


def _resolve_pointer(payload: Any, pointer: str) -> Any:
    """按 RFC6901 JSON Pointer 切片（``/data/overview``）；缺失返回 ``_MISSING``。"""
    if not pointer.startswith("/"):
        return _MISSING
    current = payload
    for raw_part in pointer.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                return _MISSING
            current = current[int(part)]
        else:
            return _MISSING
    return current


# --------------------------------------------------------------------------- #
# read_artifact
# --------------------------------------------------------------------------- #


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(min_length=1)
    version: int | None = None
    section: str | None = None


class ReadArtifactTool:
    """读取 Artifact payload（已发布 Version 或活动 Draft；只读、零积分）。

    - 默认读最新已发布 Version（``status="published"``）；Artifact 有活动
      Draft（drafting/reviewing，如 Builder 刚产出待审核）时读当前 Draft
      Revision（``status="draft"`` + revision 号），显式 ``version`` 恒读
      已发布 Version；
    - ``section``：Draft 按 RFC6901（``/data/overview``）切片，已发布
      Version 按点分路径（``data.overview``）切片。
    """

    name = "read_artifact"
    input_model = ReadArtifactArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args, parse_error = _parse_args(ReadArtifactArgs, arguments)
        if parse_error is not None:
            return parse_error
        _session, error = await _owned_session(self._db, context)
        if error is not None:
            return _failed(error, "session_" + error)

        artifact = await self._db.get(AgentArtifact, args.artifact_id)
        if artifact is None:
            return _failed(NOT_FOUND, "artifact_not_found")
        if artifact.user_id != context.user_id:
            return _failed(FORBIDDEN, "artifact_forbidden")
        # 跨 Session 只允许读已发布 Version（§5.4 历史复用）；活动 Draft 仍限本
        # Session，由 _try_read_draft 的 session 校验兜底，跨 Session 走已发布路径。

        if args.version is None:
            draft_result = await self._try_read_draft(context, artifact, args.section)
            if draft_result is not None:
                return draft_result

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
                "status": "published",
                "version": version,
                "section": section,
                "payload": payload,
            },
            ensure_ascii=False,
        )
        return ToolResult(status="success", safe_summary=summary)

    async def _try_read_draft(
        self, context: ToolContext, artifact: AgentArtifact, section: str | None
    ) -> ToolResult | None:
        """活动 Draft 存在时读当前 Draft Revision；否则返回 None 走已发布路径。"""
        draft = await self._db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.artifact_id == artifact.id)
        )
        if (
            draft is None
            or draft.session_id != context.session_id
            or draft.status not in _ACTIVE_DRAFT_STATUSES
            or draft.current_revision < 1
        ):
            return None
        revision = await self._db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
            )
        )
        if revision is None:
            return None

        payload: Any = revision.payload_json if revision.payload_json is not None else {}
        section_out: str | None = None
        if section:
            payload = _resolve_pointer(payload, section)
            if payload is _MISSING:
                return _failed(NOT_FOUND, "artifact_section_not_found")
            section_out = section

        summary = json.dumps(
            {
                "artifact_id": artifact.id,
                "status": "draft",
                "draft_id": draft.id,
                "revision": revision.revision,
                "schema_version": revision.schema_version,
                "section": section_out,
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
        args, parse_error = _parse_args(SearchEvidenceArgs, arguments)
        if parse_error is not None:
            return parse_error
        _session, error = await _owned_session(self._db, context)
        if error is not None:
            return _failed(error, "session_" + error)

        conditions = [EvidenceItem.session_id == context.session_id]
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

        # 只投影匹配/展示所需列（不加载大字段 raw_payload_json），并加 SQL LIMIT
        # 限制单次扫描量，避免长 Session 无限加载。
        result = await self._db.execute(
            select(*_EVIDENCE_MATCH_COLUMNS)
            .where(*conditions)
            .order_by(EvidenceItem.collected_at.desc(), EvidenceItem.id.desc())
            .limit(_SEARCH_SCAN_LIMIT)
        )
        rows = result.all()
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
    # §10.2：limit 上限 200，防御超大 limit 整页返回大结果。
    limit: int = Field(
        default=_TOOL_RESULT_DEFAULT_LIMIT, ge=1, le=_TOOL_RESULT_MAX_LIMIT
    )


class ReadToolResultTool:
    """按游标分片读取 Evidence 原始结果；大结果绝不整页返回（§10.2）。"""

    name = "read_tool_result"
    input_model = ReadToolResultArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args, parse_error = _parse_args(ReadToolResultArgs, arguments)
        if parse_error is not None:
            return parse_error
        _session, error = await _owned_session(self._db, context)
        if error is not None:
            return _failed(error, "session_" + error)

        evidence = await self._db.get(EvidenceItem, args.evidence_id)
        if evidence is None or evidence.session_id != context.session_id:
            # 不泄露跨 Session evidence 的存在性。
            return _failed(NOT_FOUND, "evidence_not_found")

        sequence, total = self._sequence(evidence.raw_payload_json)
        offset = max(args.cursor or 0, 0)
        # 防御性钳制：即使未来字段放宽，页大小也绝不超过上限。
        limit = min(max(args.limit, 1), _TOOL_RESULT_MAX_LIMIT)
        page = sequence[offset : offset + limit]
        next_offset = offset + limit
        next_cursor = str(next_offset) if next_offset < total else None
        truncated = next_cursor is not None
        view = build_model_evidence_view(evidence)
        summary = json.dumps(
            {
                "evidence_id": evidence.id,
                "items": page,
                "total": total,
                "next_cursor": next_cursor,
                "truncated": truncated,
                # 统一 normalization 诊断（Gate B P1：恢复/即时返回/钻取一致）。
                "normalization_status": view["normalization_status"],
                "field_mapping": view["field_mapping"],
                "unmapped_fields": view["unmapped_fields"],
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


# --------------------------------------------------------------------------- #
# remember_scope
# --------------------------------------------------------------------------- #


class RememberScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal["brand", "campaign", "kol"]
    # 拒绝空 values：无字段可确认的调用是模型幻觉，直接结构化失败（Gate A 审查）。
    values: dict[str, JsonValue] = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    explicit: bool = True


class RememberScopeTool:
    """持久化用户已确认的范围条件（``confirmed_scope`` 记忆；零积分）。

    按 ``values`` 的每个 field 落一条 ``confirmed_scope`` MemoryEntry；同
    domain+field 的旧 active 条目在同一事务里被 supersede（保留审计历史），
    Context Builder 只注入未 supersede 条目。

    ``source_message_id`` 必须存在且属于当前 Session 的用户消息（Gate A
    审查：校验来源消息归属，不允许引用他人/他 Session 消息）。
    """

    name = "remember_scope"
    input_model = RememberScopeArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args, parse_error = _parse_args(RememberScopeArgs, arguments)
        if parse_error is not None:
            return parse_error
        _session, error = await _owned_session(self._db, context)
        if error is not None:
            return _failed(error, "session_" + error)

        source = await self._db.scalar(
            select(AgentMessage.id).where(
                AgentMessage.id == args.source_message_id,
                AgentMessage.session_id == context.session_id,
                AgentMessage.role == "user",
            )
        )
        if source is None:
            return _failed(
                NOT_FOUND,
                f"source message {args.source_message_id!r} not found in this session",
            )

        now = utc_now()
        superseded = 0
        for field, value in args.values.items():
            previous = await self._db.scalars(
                select(MemoryEntry).where(
                    MemoryEntry.session_id == context.session_id,
                    MemoryEntry.memory_type == "confirmed_scope",
                    MemoryEntry.superseded_at.is_(None),
                    MemoryEntry.content_json["domain"].as_string() == args.domain,
                    MemoryEntry.content_json["field"].as_string() == field,
                )
            )
            for entry in previous:
                entry.superseded_at = now
                superseded += 1
            self._db.add(
                MemoryEntry(
                    id=str(uuid4()),
                    session_id=context.session_id,
                    source_run_id=context.run_id,
                    memory_type="confirmed_scope",
                    content_json={
                        "domain": args.domain,
                        "field": field,
                        "value": value,
                        "source_message_id": args.source_message_id,
                        "explicit": args.explicit,
                    },
                    created_at=now,
                )
            )
        await self._db.flush()
        summary = json.dumps(
            {
                "domain": args.domain,
                "remembered": dict(args.values),
                "superseded": superseded,
            },
            ensure_ascii=False,
        )
        return ToolResult(status="success", safe_summary=summary)


__all__ = [
    "FORBIDDEN",
    "INVALID_ARGUMENTS",
    "NOT_FOUND",
    "ReadArtifactArgs",
    "ReadArtifactTool",
    "ReadToolResultArgs",
    "ReadToolResultTool",
    "RememberScopeArgs",
    "RememberScopeTool",
    "SearchEvidenceArgs",
    "SearchEvidenceTool",
]
