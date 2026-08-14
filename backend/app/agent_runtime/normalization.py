"""DataTap 成功 payload 字段映射归一化诊断（Gate B）。

``NormalizationRegistry`` 把 MCP 成功结果的业务字段映射为规范键（period_key、
volume 等），未识别字段进入 ``unmapped_fields`` 诊断。状态 ``incomplete``
表示字段未完全映射——原始 payload 仍完整落库为 Evidence，诊断只是提示
下游，绝不把「有数据但字段未识别」误报为「无 Evidence」。

工具无专用 adapter 时返回 ``not_applicable``（同样不误报失败）；adapter
异常返回 ``failed`` + error_code。Registry 是纯函数模块，不依赖数据库。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from app.agent_artifacts.builders.raw_rows import (
    TIME_KEYS,
    VOLUME_KEYS,
    canonicalize_marketing_evidence,
    num,
    text,
    unwrap_payload,
)

logger = logging.getLogger(__name__)

JsonValue = Any

_VERSION = "normalization_v1"

# 归一化 preview 的最大行数：超出截断（原始 payload 不受影响）。
_MAX_PREVIEW_ROWS = 5_000

# 行容器键（与 raw_rows.extract_rows 同源）。
_ROW_CONTAINER_KEYS = ("rows", "list", "items", "data", "posts", "records")


@dataclass(frozen=True)
class NormalizationResult:
    version: str
    status: Literal["normalized", "incomplete", "not_applicable", "failed"]
    preview: dict[str, Any] | list[Any] | None
    field_mapping: dict[str, str]
    unmapped_fields: tuple[str, ...]
    truncated: bool
    error_code: str | None = None


def _extract_rows(payload: JsonValue) -> list[dict[str, Any]] | None:
    """从 DataTap payload 提取行列表；先解 {"result":"<json>"} 包装。"""
    unwrapped, _ = unwrap_payload(payload)
    if isinstance(unwrapped, list):
        return [item for item in unwrapped if isinstance(item, dict)]
    if isinstance(unwrapped, dict):
        for key in _ROW_CONTAINER_KEYS:
            value = unwrapped.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return None


def _normalize_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str], set[str]]:
    """每行映射规范键；返回 (preview 行, 列名→规范键, 未识别列名集合)。"""
    preview_rows: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    unmapped: set[str] = set()
    for row in rows:
        normalized: dict[str, Any] = {}
        for key, value in row.items():
            if value is None or value == "":
                continue
            if key in TIME_KEYS:
                normalized["period_key"] = text(value)
                mapping[key] = "period_key"
            elif key in VOLUME_KEYS:
                parsed = num(value)
                if parsed is not None:
                    normalized["volume"] = (
                        int(parsed) if parsed.is_integer() else parsed
                    )
                    mapping[key] = "volume"
            else:
                unmapped.add(key)
        preview_rows.append(normalized)
    return preview_rows, mapping, unmapped


def _trend_normalizer(payload: JsonValue) -> NormalizationResult:
    """时间序列工具（query_analysis_data / social_statistic_trend）适配器。"""
    rows = _extract_rows(payload)
    if rows is None:
        return NormalizationResult(
            version=_VERSION,
            status="not_applicable",
            preview=None,
            field_mapping={},
            unmapped_fields=(),
            truncated=False,
        )
    preview_rows, mapping, unmapped = _normalize_rows(rows)
    truncated = len(preview_rows) > _MAX_PREVIEW_ROWS
    preview: dict[str, Any] = {
        "rows": preview_rows[:_MAX_PREVIEW_ROWS],
        "row_count": len(preview_rows),
        "truncated": truncated,
    }
    return NormalizationResult(
        version=_VERSION,
        status="incomplete" if unmapped else "normalized",
        preview=preview,
        field_mapping=mapping,
        unmapped_fields=tuple(sorted(unmapped)),
        truncated=truncated,
    )


class NormalizationRegistry:
    """按工具名分派的字段映射诊断注册表。"""

    def __init__(self) -> None:
        self._adapters: dict[str, Callable[[JsonValue], NormalizationResult]] = {
            "query_analysis_data": _trend_normalizer,
            "social_statistic_trend": _trend_normalizer,
        }

    def normalize(self, tool_name: str, payload: JsonValue) -> NormalizationResult:
        adapter = self._adapters.get(tool_name)
        if adapter is None:
            return NormalizationResult(
                version=_VERSION,
                status="not_applicable",
                preview=None,
                field_mapping={},
                unmapped_fields=(),
                truncated=False,
            )
        try:
            return adapter(payload)
        except Exception:
            logger.exception("normalization failed for tool %s", tool_name)
            return NormalizationResult(
                version=_VERSION,
                status="failed",
                preview=None,
                field_mapping={},
                unmapped_fields=(),
                truncated=False,
                error_code="normalization_failed",
            )


__all__ = [
    "NormalizationRegistry",
    "NormalizationResult",
    "canonicalize_marketing_evidence",
]
