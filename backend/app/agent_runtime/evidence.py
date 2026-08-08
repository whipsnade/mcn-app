"""不可变 Evidence 写入器（设计文档 §8.1 / §10.2「大结果处理」）。

MCP 原始结果完整落 ``evidence_items.raw_payload_json``，并计算
``payload_hash`` 与受限 ``normalized_preview_json``。模型只通过只读工具获取
``evidence_id`` + 结构化预览，绝不接触完整原始 payload。

写入器只提供插入（append-only），**没有 update 路径**：Evidence 不可变，
历史数字来源保持稳定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import EvidenceItem
from app.mcp_gateway.validation import canonical_json_bytes

if TYPE_CHECKING:
    # 仅类型检查用：运行时 write() 对 normalization 鸭子类型访问，避免
    # evidence → normalization → builders 包 → tools → mcp → evidence
    # 的循环导入（builders/__init__ 与 tools/__init__ 均 eager import）。
    from app.agent_runtime.normalization import NormalizationResult

# 预览截断上限（§10.2：返回模型的是受控预览而非原始全文）。
_MAX_PREVIEW_STRING = 2_000
_MAX_PREVIEW_ARRAY = 50
_MAX_PREVIEW_PROPS = 200
_MAX_PREVIEW_DEPTH = 6


@dataclass(frozen=True)
class CanonicalField:
    """可追溯的营销业务字段；缺失值必须显式标记为 unavailable。"""

    path: str
    value: Any
    availability: str
    evidence_ids: tuple[str, ...]
    unit: str | None

    def model_dump(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "value": self.value,
            "availability": self.availability,
            "evidence_ids": list(self.evidence_ids),
            "unit": self.unit,
        }


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _truncate_value(value: Any, *, depth: int = 0) -> tuple[Any, bool]:
    """递归生成受限预览；返回 (截断后的值, 是否有内容被截断)。"""
    if depth > _MAX_PREVIEW_DEPTH:
        return {"__truncated__": True}, True
    if isinstance(value, str):
        if len(value) > _MAX_PREVIEW_STRING:
            return value[:_MAX_PREVIEW_STRING], True
        return value, False
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        truncated = False
        for index, (key, child) in enumerate(value.items()):
            if index >= _MAX_PREVIEW_PROPS:
                truncated = True
                break
            result[key], child_truncated = _truncate_value(child, depth=depth + 1)
            truncated = truncated or child_truncated
        return result, truncated
    if isinstance(value, list):
        result = []
        truncated = False
        for index, item in enumerate(value):
            if index >= _MAX_PREVIEW_ARRAY:
                truncated = True
                break
            child, child_truncated = _truncate_value(item, depth=depth + 1)
            truncated = truncated or child_truncated
            result.append(child)
        return result, truncated
    return value, False


def _row_count(payload: Any) -> int:
    parsed = unwrap_evidence_payload(payload)
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        rows = parsed.get("rows")
        if isinstance(rows, list):
            return len(rows)
        total = parsed.get("total")
        if isinstance(total, int):
            return total
    if isinstance(payload, list):
        return len(payload)
    return 0


def _available_fields(payload: Any) -> list[str]:
    parsed = unwrap_evidence_payload(payload)
    if isinstance(parsed, dict):
        return sorted(parsed.keys())
    if isinstance(payload, dict):
        return sorted(payload.keys())
    return []


def build_preview(raw_payload: Any) -> dict[str, Any]:
    """构造受限结构化预览（§10.2：预览 + 行数 + 截断标记 + 可用字段）。

    DataTap 结果常为 ``{result: "<json 字符串>"}``：用共享 ``unwrap_evidence_payload``
    解析内部结构后做截断，让行数/字段与预览基于同一份真实数据。
    """
    target = unwrap_evidence_payload(raw_payload)
    preview, truncated = _truncate_value(target)
    return {
        "preview": preview,
        "row_count": _row_count(raw_payload),
        "truncated": truncated,
        "available_fields": _available_fields(raw_payload),
        "payload_hash": hashlib.sha256(canonical_json_bytes(raw_payload)).hexdigest(),
    }


# 模型视图严格有界（§10.2 / Gate B）：所有消费方（MCP 即时/Transcript 恢复/
# search_evidence/read_tool_result/upload 钻取）共用同一视图，绝不重新归一化。
_MAX_MODEL_DEPTH = 6
_MAX_MODEL_ARRAY_ROWS = 200
_MAX_MODEL_OBJECT_FIELDS = 200
_MAX_MODEL_STR_LEN = 1000
_MAX_MODEL_TOTAL_CHARS = 50_000


def unwrap_evidence_payload(payload: Any) -> Any:
    """解包 DataTap 常见 ``{result: "<json 字符串>"}`` 包装；解析失败返回原值。

    build_preview 与 read_tool_result 的 _sequence 使用同一入口，避免各自写一套
    不一致的 unwrap 逻辑。
    """
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        try:
            return json.loads(payload["result"])
        except (TypeError, ValueError):
            return payload
    return payload


def _bound_model_value(value: Any, *, depth: int = 0) -> tuple[Any, bool]:
    """递归有界截断；返回 (值, 是否截断)。截断显式向上传递，调用方据此置
    ``truncated=true``，绝不静默裁剪。"""
    if depth > _MAX_MODEL_DEPTH:
        return {"__truncated__": True}, True
    if isinstance(value, str):
        if len(value) > _MAX_MODEL_STR_LEN:
            return value[:_MAX_MODEL_STR_LEN], True
        return value, False
    if isinstance(value, list):
        truncated = len(value) > _MAX_MODEL_ARRAY_ROWS
        items: list[Any] = []
        for index, item in enumerate(value):
            if index >= _MAX_MODEL_ARRAY_ROWS:
                break
            child, child_truncated = _bound_model_value(item, depth=depth + 1)
            items.append(child)
            truncated = truncated or child_truncated
        return items, truncated
    if isinstance(value, dict):
        truncated = len(value) > _MAX_MODEL_OBJECT_FIELDS
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= _MAX_MODEL_OBJECT_FIELDS:
                break
            bounded_child, child_truncated = _bound_model_value(child, depth=depth + 1)
            result[key] = bounded_child
            truncated = truncated or child_truncated
        return result, truncated
    return value, False


def bound_model_value(value: Any) -> tuple[Any, bool]:
    """对任意值做模型可见的有界截断；返回 (值, 是否截断)。"""
    return _bound_model_value(value)


def model_response_size(value: Any) -> int:
    """模型可见负载的序列化字符数（所有硬预算检查共用的测量入口）。"""
    return len(json.dumps(value, ensure_ascii=False, default=str))


def fit_dict_by_chars(mapping: Any, *, max_chars: int) -> tuple[Any, bool]:
    """按序列化字符预算逐条保留 dict 条目（保持顺序）；返回 (值, 是否截断)。"""
    if not isinstance(mapping, dict):
        return mapping, False
    result: dict[str, Any] = {}
    truncated = False
    for key, child in mapping.items():
        candidate = {**result, key: child}
        if model_response_size(candidate) > max_chars:
            truncated = True
            break
        result = candidate
    return result, truncated


def fit_list_by_chars(values: Any, *, max_chars: int) -> tuple[Any, bool]:
    """按序列化字符预算逐条保留 list 元素（保持顺序）；返回 (值, 是否截断)。"""
    if not isinstance(values, list):
        return values, False
    result: list[Any] = []
    truncated = False
    for item in values:
        candidate = [*result, item]
        if model_response_size(candidate) > max_chars:
            truncated = True
            break
        result.append(item)
    return result, truncated


def build_model_evidence_view(evidence: EvidenceItem) -> dict[str, Any]:
    """统一模型可见视图——**唯一**模型视图入口。

    从已持久化的 ``normalized_preview_json`` 构建**有界**视图，绝不重新归一化；
    预览优先级：``normalization_preview`` 优先，否则回退 raw ``preview``。preview
    与 field_mapping/unmapped_fields **共同参与** 50KB 总预算：先各自递归裁剪，
    仍超预算则按顺序降级（preview 截断哨兵 → 诊断按字符预算保留部分 → 诊断清空），
    truncated=true 明确数据被裁剪。**始终保留全部 9 个固定键**，任何情况下都能
    ``json.loads()``。
    """
    stored = evidence.normalized_preview_json or {}
    normalization_preview = stored.get("normalization_preview")
    raw_preview = stored.get("preview")
    source_preview = (
        normalization_preview if normalization_preview is not None else raw_preview
    )
    preview, preview_truncated = _bound_model_value(source_preview)
    field_mapping, mapping_truncated = _bound_model_value(stored.get("field_mapping"))
    unmapped_fields, unmapped_truncated = _bound_model_value(stored.get("unmapped_fields"))
    base_truncated = bool(
        stored.get("truncated")
        or preview_truncated
        or mapping_truncated
        or unmapped_truncated
    )

    def _view(preview_value: Any, mapping_value: Any, unmapped_value: Any, truncated: bool) -> dict[str, Any]:
        return {
            "evidence_id": evidence.id,
            "preview": preview_value,
            "normalization_status": stored.get("normalization_status"),
            "field_mapping": mapping_value,
            "unmapped_fields": unmapped_value,
            "row_count": stored.get("row_count", 0),
            "truncated": truncated,
            "source_name": evidence.source_name,
            "source_type": evidence.source_type,
        }

    view = _view(preview, field_mapping, unmapped_fields, base_truncated)
    if model_response_size(view) <= _MAX_MODEL_TOTAL_CHARS:
        return view

    # 降级 1：preview 改截断哨兵（truncated=true 明确数据被裁剪）。
    view = _view({"__truncated__": True}, field_mapping, unmapped_fields, True)
    if model_response_size(view) <= _MAX_MODEL_TOTAL_CHARS:
        return view

    # 降级 2：field_mapping/unmapped_fields 按剩余字符预算保留部分内容。
    base_without_diag = _view({"__truncated__": True}, {}, [], True)
    remaining = _MAX_MODEL_TOTAL_CHARS - model_response_size(base_without_diag)
    half = max(remaining // 2, 0)
    fitted_mapping, _ = fit_dict_by_chars(field_mapping, max_chars=half)
    rest = max(remaining - model_response_size(fitted_mapping), 0)
    fitted_unmapped, _ = fit_list_by_chars(unmapped_fields, max_chars=rest)
    view = _view({"__truncated__": True}, fitted_mapping, fitted_unmapped, True)
    if model_response_size(view) <= _MAX_MODEL_TOTAL_CHARS:
        return view

    # 降级 3：诊断清空（truncated=true 明确数据被裁剪）；最小固定形状必然 <= 预算。
    return _view({"__truncated__": True}, {}, [], True)


class EvidenceWriter:
    """不可变 Evidence 写入器：仅插入（append-only），不提供更新路径。"""

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def write(
        self,
        *,
        session_id: str,
        run_id: str | None,
        tool_call_id: str | None = None,
        upload_id: str | None = None,
        source_type: str,
        source_name: str,
        scope_json: dict[str, Any] | None,
        period_json: dict[str, Any] | None,
        raw_payload: Any,
        collected_at: datetime | None = None,
        availability_status: str = "available",
        normalization: NormalizationResult | None = None,
    ) -> EvidenceItem:
        # Evidence 必须且只能关联 tool_call_id 或 upload_id 之一（DB XOR 约束）。
        if (tool_call_id is None) == (upload_id is None):
            raise ValueError("exactly one of tool_call_id/upload_id is required")
        preview = build_preview(raw_payload)
        # 统一模型视图：raw preview + normalization 诊断合并为单个 JSON，
        # 模型经 read_tool_result / search_evidence / Transcript 恢复消费同一视图。
        if normalization is not None:
            preview["normalization_status"] = normalization.status
            preview["normalization_preview"] = normalization.preview
            preview["field_mapping"] = normalization.field_mapping
            preview["unmapped_fields"] = list(normalization.unmapped_fields)
        item = EvidenceItem(
            id=str(uuid4()),
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            upload_id=upload_id,
            source_type=source_type,
            source_name=source_name,
            scope_json=scope_json,
            period_json=period_json,
            raw_payload_json=raw_payload,
            normalized_preview_json=preview,
            payload_hash=preview["payload_hash"],
            collected_at=collected_at or _now(),
            availability_status=availability_status,
            truncated=bool(preview["truncated"] or (normalization.truncated if normalization else False)),
            normalization_version=normalization.version if normalization else None,
            normalization_status=normalization.status if normalization else None,
            field_mapping_json=normalization.field_mapping if normalization else None,
            unmapped_fields_json=(
                list(normalization.unmapped_fields) if normalization else None
            ),
            normalization_error_code=normalization.error_code if normalization else None,
        )
        self._db.add(item)
        await self._db.flush()
        return item

    async def get_by_tool_call_id(self, tool_call_id: str) -> EvidenceItem | None:
        """只读读取：模型通过工具拿到的证据查询也只返回模型视图。"""
        return await self._db.scalar(
            select(EvidenceItem)
            .where(EvidenceItem.tool_call_id == tool_call_id)
            .order_by(EvidenceItem.collected_at.desc())
        )


__all__ = [
    "CanonicalField",
    "EvidenceWriter",
    "bound_model_value",
    "build_model_evidence_view",
    "build_preview",
    "fit_dict_by_chars",
    "fit_list_by_chars",
    "model_response_size",
    "unwrap_evidence_payload",
]
