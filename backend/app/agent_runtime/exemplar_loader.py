"""受控成功案例加载器（Gate B：品牌分析成功案例回放）。

只读取包内 allowlist 文件（``exemplars/`` 目录下的受控 JSON 资产），不接受
用户路径。加载时做结构/大小校验，失败记录 warning 并降级为空列表，绝不
阻塞 Run。注入模型的投影只保留策略字段，删除 ``source`` 与
``forbidden_copy_values``——保证来源案例中的品牌名、固定日期和真实汇总值
永远不会进入 Prompt（参数化校验在测试中冻结）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# allowlist：只允许包内受控文件，用户路径一律拒绝。
_ALLOWED_FILES = ("brand_analysis_success_v1.json",)

# 注入模型的投影字段（必须与计划契约一致；source/forbidden_copy_values 排除）。
_PROJECTION_KEYS = (
    "applicable_when",
    "parameters",
    "successful_strategy",
    "decision_rules",
    "coverage_targets",
    "completion_contract",
)

# 必填结构键：缺任一视为损坏。
_REQUIRED_KEYS = (
    "exemplar_id",
    "version",
    "purpose",
    "applicable_when",
    "parameters",
    "successful_strategy",
    "decision_rules",
)

_MAX_JSON_BYTES = 64 * 1024


def _read_curated_exemplar(filename: str) -> dict[str, Any]:
    if filename not in _ALLOWED_FILES:
        raise ValueError(f"curated exemplar not in allowlist: {filename}")
    path = Path(__file__).parent / "exemplars" / filename
    raw = path.read_bytes()
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"curated exemplar too large: {filename}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"curated exemplar is not an object: {filename}")
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError(f"curated exemplar missing keys {missing}: {filename}")
    return data


def load_curated_exemplars(purpose: str, limit: int = 1) -> list[dict[str, Any]]:
    """按 purpose 加载受控策略案例；失败降级为空列表，不抛出。"""
    results: list[dict[str, Any]] = []
    for filename in _ALLOWED_FILES:
        try:
            exemplar = _read_curated_exemplar(filename)
        except Exception:
            logger.warning("failed to load curated exemplar %s", filename, exc_info=True)
            continue
        if exemplar.get("purpose") != purpose:
            continue
        projection = {
            key: exemplar[key] for key in _PROJECTION_KEYS if key in exemplar
        }
        projection["exemplar_id"] = exemplar["exemplar_id"]
        projection["kind"] = "curated_strategy"
        results.append(projection)
        if len(results) >= limit:
            break
    return results


__all__ = ["load_curated_exemplars"]
