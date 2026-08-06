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


def _parsed_result(payload: Any) -> Any:
    """DataTap 结果常为 {result: "<json 字符串>"}；尝试解析出内部结构。"""
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str):
            try:
                return json.loads(result)
            except (TypeError, ValueError):
                return None
    return None


def _row_count(payload: Any) -> int:
    parsed = _parsed_result(payload)
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
    parsed = _parsed_result(payload)
    if isinstance(parsed, dict):
        return sorted(parsed.keys())
    if isinstance(payload, dict):
        return sorted(payload.keys())
    return []


def build_preview(raw_payload: Any) -> dict[str, Any]:
    """构造受限结构化预览（§10.2：预览 + 行数 + 截断标记 + 可用字段）。

    DataTap 结果常为 ``{result: "<json 字符串>"}``：解析内部结构后做截断，
    让行数/字段与预览基于同一份真实数据。
    """
    parsed = _parsed_result(raw_payload)
    target = parsed if parsed is not None else raw_payload
    preview, truncated = _truncate_value(target)
    return {
        "preview": preview,
        "row_count": _row_count(raw_payload),
        "truncated": truncated,
        "available_fields": _available_fields(raw_payload),
        "payload_hash": hashlib.sha256(canonical_json_bytes(raw_payload)).hexdigest(),
    }


def model_view(item: EvidenceItem) -> dict[str, Any]:
    """模型可见视图：只暴露 evidence_id + 受限预览，绝不含完整原始 payload。"""
    return {
        "evidence_id": item.id,
        "preview": item.normalized_preview_json,
    }


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
    "EvidenceWriter",
    "build_preview",
    "model_view",
]
