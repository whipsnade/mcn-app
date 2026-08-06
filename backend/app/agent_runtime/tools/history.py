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

import base64
import binascii
import json
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypeAliasType

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
)
from app.agent_runtime.evidence import (
    bound_model_value,
    build_model_evidence_view,
    fit_dict_by_chars,
    fit_list_by_chars,
    model_response_size,
    unwrap_evidence_payload,
)
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
# 模型可见工具响应的总字符预算（Gate B P1-3）：200 个最大 item 合并可达 40MB，
# 必须按总预算逐项构建页面，任何响应都 ≤ 该预算。
_MAX_TOOL_RESULT_TOTAL_CHARS = 50_000
# read_tool_result 解包后支持的行容器键。
_SEQUENCE_CONTAINER_KEYS = ("rows", "list", "items", "data", "posts", "records")
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
    # query 有界：响应中原样回显，60KB query 会让整个响应超预算（Gate B P1）。
    query: str = Field(default="", max_length=500)
    artifact_id: str | None = None
    filters: dict[str, Any] | None = None
    # keyset 游标（受控 base64 格式）；超长/无法解析返回 invalid_arguments。
    cursor: str | None = Field(default=None, max_length=200)


def _encode_search_cursor(collected_at: datetime, evidence_id: str) -> str:
    """把最后一个已消费 Evidence 的 (collected_at, id) 编码为受控 cursor。"""
    payload = json.dumps(
        {"t": collected_at.isoformat(), "i": evidence_id}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_search_cursor(raw: str) -> tuple[datetime, str] | None:
    """解析 cursor；任何格式错误返回 None（调用方映射 invalid_arguments）。"""
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    raw_t = payload.get("t")
    raw_id = payload.get("i")
    if not isinstance(raw_t, str) or not isinstance(raw_id, str) or not raw_id:
        return None
    try:
        return datetime.fromisoformat(raw_t), raw_id
    except ValueError:
        return None


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
        # keyset 游标：保持 collected_at DESC, id DESC 顺序，只取该游标之后（更旧）
        # 的 Evidence；伪造 cursor 只能得到受控 invalid_arguments，绝不跨 Session。
        if args.cursor is not None:
            decoded = _decode_search_cursor(args.cursor)
            if decoded is None:
                return _failed(INVALID_ARGUMENTS, "invalid search cursor")
            cursor_collected_at, cursor_id = decoded
            conditions.append(
                or_(
                    EvidenceItem.collected_at < cursor_collected_at,
                    and_(
                        EvidenceItem.collected_at == cursor_collected_at,
                        EvidenceItem.id < cursor_id,
                    ),
                )
            )

        # 只投影匹配/展示所需列（不加载大字段 raw_payload_json）；固定扫描窗口
        # 500+1：第 501 条仅用于探测后面是否还有数据，绝不一次加载整个 Session。
        result = await self._db.execute(
            select(*_EVIDENCE_MATCH_COLUMNS)
            .where(*conditions)
            .order_by(EvidenceItem.collected_at.desc(), EvidenceItem.id.desc())
            .limit(_SEARCH_SCAN_LIMIT + 1)
        )
        rows = result.all()

        # 固定字段 + matches + 每个 view 共同参与 50KB 总预算（20 × 50KB 不可接受）。
        fixed = model_response_size(
            {
                "query": args.query,
                "scanned_count": _SEARCH_SCAN_LIMIT,
                "returned_matches": _SEARCH_MATCH_LIMIT,
                "next_cursor": "x" * 200,
                "has_more": False,
                "view_truncated": False,
                "truncated": False,
                "matches": [],
            }
        )
        matches_budget = max(_MAX_TOOL_RESULT_TOTAL_CHARS - fixed, 1)
        page: list[dict[str, Any]] = []
        consumed = 0
        view_truncated = False
        for item in rows[:_SEARCH_SCAN_LIMIT]:
            if not self._matches(item, args.query or ""):
                consumed += 1
                continue
            full_match = {
                "evidence_id": item.id,
                "source_type": item.source_type,
                "source_name": item.source_name,
                "run_id": item.run_id,
                "collected_at": item.collected_at.isoformat() if item.collected_at else None,
                # 统一有界模型视图（Gate B：不返回完整 5000 行）。
                "view": build_model_evidence_view(item),
            }
            if model_response_size([*page, full_match]) <= matches_budget:
                page.append(full_match)
                consumed += 1
            else:
                # 完整 view 放不下：退回最小 match（含 evidence_id，模型仍可
                # read_tool_result 钻取原始数据）。
                minimal_match = {
                    "evidence_id": item.id,
                    "source_type": item.source_type,
                    "source_name": item.source_name,
                    "run_id": item.run_id,
                    "collected_at": item.collected_at.isoformat() if item.collected_at else None,
                    "view_omitted": True,
                }
                if model_response_size([*page, minimal_match]) <= matches_budget:
                    page.append(minimal_match)
                    view_truncated = True
                    consumed += 1
                else:
                    # 预算连最小 stub 都放不下：**不消费**该 Evidence，下一页重新
                    # 出现（next_cursor 只能指向已消费项，绝不跳过未返回 match）。
                    break
            if len(page) >= _SEARCH_MATCH_LIMIT:
                break

        # cursor 只按实际消费的源 Evidence 推进（scanned_count 个），不允许直接
        # 推进到 SQL 批次末尾——预算截掉的行必须在下页重新出现。
        has_more = consumed < len(rows)
        next_cursor: str | None = None
        if has_more and consumed > 0:
            last = rows[consumed - 1]
            if last.collected_at is not None:
                next_cursor = _encode_search_cursor(last.collected_at, last.id)
        summary_payload = {
            "query": args.query,
            "scanned_count": consumed,
            "returned_matches": len(page),
            "next_cursor": next_cursor,
            "has_more": has_more,
            "view_truncated": view_truncated,
            "truncated": has_more or view_truncated,
            "matches": page,
        }
        # 逐项预算校验保证整个响应 <= 50KB（占位测量固定字段为最保守值）。
        summary = json.dumps(summary_payload, ensure_ascii=False)
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
        view = build_model_evidence_view(evidence)
        # 诊断字段（field_mapping/unmapped_fields）来自统一视图，但最多占用预算的
        # 固定份额，保证 items 始终有可用空间（固定元数据与 items 同一总预算）。
        diagnostics_budget = _MAX_TOOL_RESULT_TOTAL_CHARS // 5
        field_mapping, mapping_truncated = fit_dict_by_chars(
            view["field_mapping"], max_chars=diagnostics_budget
        )
        remaining_diag = max(diagnostics_budget - model_response_size(field_mapping), 0)
        unmapped_fields, unmapped_truncated = fit_list_by_chars(
            view["unmapped_fields"], max_chars=remaining_diag
        )
        diagnostics_truncated = mapping_truncated or unmapped_truncated
        # 固定开销（元数据 + 统一诊断）用最保守占位测量，items_budget 为剩余预算。
        fixed = len(
            json.dumps(
                {
                    "evidence_id": evidence.id,
                    "items": [],
                    "total": total,
                    "next_cursor": str(total),
                    "truncated": False,
                    "normalization_status": view["normalization_status"],
                    "field_mapping": field_mapping,
                    "unmapped_fields": unmapped_fields,
                },
                ensure_ascii=False,
            )
        )
        items_budget = max(_MAX_TOOL_RESULT_TOTAL_CHARS - fixed, 1)
        # 逐项构建页面：加入 candidate 后超过总预算即停止；cursor 按实际消费的
        # 源数据行数推进（被字符预算截掉的行走源 index，绝不丢失）。
        items: list[Any] = []
        source_index = offset
        item_truncated_any = False
        while source_index < total and len(items) < limit:
            bounded_item, item_truncated = bound_model_value(sequence[source_index])
            item_truncated_any = item_truncated_any or item_truncated
            candidate = [*items, bounded_item]
            if model_response_size(candidate) > items_budget:
                if not items:
                    # 单个超大 item：返回合法占位并把 cursor 前进一行，避免同一
                    # 超大 item 造成无限循环。
                    items = [
                        {"__truncated__": True, "__reason__": "item_exceeds_model_budget"}
                    ]
                    source_index += 1
                    item_truncated_any = True
                    break
                break
            items = candidate
            source_index += 1
        next_cursor = str(source_index) if source_index < total else None
        truncated = next_cursor is not None or item_truncated_any or diagnostics_truncated
        summary_payload = {
            "evidence_id": evidence.id,
            "items": items,
            "total": total,
            "next_cursor": next_cursor,
            "truncated": truncated,
            # 统一 normalization 诊断（恢复/即时返回/钻取一致，有界份额）。
            "normalization_status": view["normalization_status"],
            "field_mapping": field_mapping,
            "unmapped_fields": unmapped_fields,
        }
        # 最终硬预算校验：超预算时诊断字段让位给 items（压缩到剩余预算，绝不
        # 截断 JSON 字符串本身）。
        if model_response_size(summary_payload) > _MAX_TOOL_RESULT_TOTAL_CHARS:
            base = {
                key: value
                for key, value in summary_payload.items()
                if key not in ("field_mapping", "unmapped_fields")
            }
            remaining = _MAX_TOOL_RESULT_TOTAL_CHARS - model_response_size(base)
            if remaining < 0:
                summary_payload = {
                    "evidence_id": evidence.id,
                    "items": [],
                    "total": total,
                    "next_cursor": next_cursor,
                    "truncated": True,
                    "normalization_status": None,
                    "field_mapping": {},
                    "unmapped_fields": [],
                }
            else:
                half = remaining // 2
                fitted_mapping, _ = fit_dict_by_chars(
                    field_mapping, max_chars=max(half, 0)
                )
                rest = max(remaining - model_response_size(fitted_mapping), 0)
                fitted_unmapped, _ = fit_list_by_chars(
                    unmapped_fields, max_chars=rest
                )
                summary_payload["field_mapping"] = fitted_mapping
                summary_payload["unmapped_fields"] = fitted_unmapped
                summary_payload["truncated"] = True
        summary = json.dumps(summary_payload, ensure_ascii=False)
        return ToolResult(
            status="success",
            safe_summary=summary,
            cursor=next_cursor,
            truncated=truncated,
        )

    @staticmethod
    def _sequence(raw: Any) -> tuple[list[Any], int]:
        """把原始结果（含 DataTap ``{result: "<json>"}`` 包装）归一到可分片序列。

        用共享 ``unwrap_evidence_payload`` 解包后支持顶层 list / rows / list /
        items / data / posts / records；容器值不是 list 时不伪造分页，按单个
        有界结果返回；JSON 字符串解析失败返回受控单项结果，绝不抛 500。
        """
        unwrapped = unwrap_evidence_payload(raw)
        if isinstance(unwrapped, list):
            return unwrapped, len(unwrapped)
        if isinstance(unwrapped, dict):
            for key in _SEQUENCE_CONTAINER_KEYS:
                value = unwrapped.get(key)
                if isinstance(value, list):
                    return value, len(value)
        return [unwrapped], 1


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
