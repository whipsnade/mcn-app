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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
_MAX_INJECTION_CHARS = 6000
_EXPECTED_VERSION = 1


class _CuratedStrategyStage(BaseModel):
    """成功策略的每个阶段（嵌套校验）。"""
    model_config = ConfigDict(extra="forbid")
    stage: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    preferred_capability: str = Field(min_length=1)
    success_signal: str = Field(min_length=1)
    fallback: str = Field(min_length=1)


class _CuratedExemplar(BaseModel):
    """受控 exemplar 的强类型契约（Gate B M5：嵌套 Schema 校验）。"""
    model_config = ConfigDict(extra="forbid")
    exemplar_id: str = Field(min_length=1)
    version: int
    purpose: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    language: str = Field(min_length=1)
    applicable_when: list[str] = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)
    objective: str = Field(min_length=1)
    successful_strategy: list[_CuratedStrategyStage] = Field(min_length=1)
    decision_rules: list[str] = Field(min_length=1)
    coverage_targets: list[str] = Field(min_length=1)
    completion_contract: dict[str, Any] = Field(default_factory=dict)
    forbidden_copy_values: list[str] = Field(default_factory=list)


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
    # 强类型 Pydantic 校验：嵌套结构 + extra=forbid + 必填项 + 版本
    # source 字段不在 schema 中 → extra=forbid 会拒绝（需 pop 再 validate）
    source = data.pop("source", None)
    try:
        validated = _CuratedExemplar.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"curated exemplar schema validation failed: {filename}: {exc}") from exc
    # 恢复 source 供投影使用（不进入模型上下文）
    if source is not None:
        data["source"] = source
    return validated.model_dump() | ({"source": source} if source else {})


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
        # 通用 forbidden 值扫描：投影文本不得含来源实体/日期/真实数值
        forbidden = exemplar.get("forbidden_copy_values") or []
        serialized = json.dumps(projection, ensure_ascii=False)
        if any(isinstance(v, str) and v in serialized for v in forbidden):
            logger.warning(
                "curated exemplar %s contains forbidden value in projection; skipping",
                filename,
            )
            continue
        if len(serialized) > _MAX_INJECTION_CHARS:
            logger.warning(
                "curated exemplar %s exceeds injection size %d; skipping",
                filename,
                _MAX_INJECTION_CHARS,
            )
            continue
        results.append(projection)
        if len(results) >= limit:
            break
    return results


__all__ = ["load_curated_exemplars"]
