"""Artifact Key 服务端生成规则（设计文档 §8.1 ArtifactKey / Task 12）。

模型只提供业务字段，服务端负责标准化并生成稳定 ``artifact_key``：

- 品牌：``brand:{normalized_brand}``；
- 活动：``campaign:{normalized_brand}:{normalized_campaign}``；
- 圈选名单：``kol-selection:{normalized_scope_hash}``；
- KOL 分析：``kol-analysis:{selection_artifact_id}``；
- 达人详情：``kol-detail:{platform}:{kol_uid}``；
- 钻取：``insight:{parent_artifact_version_id}:{normalized_question_hash}``。

标准化 = NFKC + trim + 连续空白折叠 + 英文小写；hash 使用 SHA-256。
``scope`` 先递归标准化所有字符串叶子再序列化，``question`` 先 ``_normalize`` 再
序列化；序列化统一用 ``sort_keys``，保证与 dict 键顺序无关。

API 只接受业务字段，不接受模型自定义 key —— 模型无法直接指定数据库 key。
business fields 的非空校验（拒绝 ``brand:`` 这类裸 key）由发布边界的
``ArtifactPayloadValidator``（``agent_artifacts/validation.py``）统一负责，
本模块只做标准化与拼接。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(value: str) -> str:
    """NFKC、trim、连续空白折叠、小写。"""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized.lower()


def _stable_hash(value: Any) -> str:
    """规范 JSON 序列化 + SHA-256；``sort_keys`` 保证 dict 键序无关。"""
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_value(value: Any) -> Any:
    """递归标准化 JSON 结构中的字符串叶子（与 ``_normalize`` 同规则）。

    非字符串叶子（数字/布尔/None）保持原样，dict 键不变，保证同一业务 scope
    即使空白/大小写不同也生成同一 hash（设计 §8.1 统一标准化规则）。
    """
    if isinstance(value, str):
        return _normalize(value)
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def build_artifact_key(
    module: str,
    *,
    brand: str | None = None,
    campaign: str | None = None,
    scope: Any | None = None,
    selection_artifact_id: str | None = None,
    platform: str | None = None,
    kol_uid: str | None = None,
    parent_artifact_version_id: str | None = None,
    question: str | None = None,
) -> str:
    """根据业务字段生成稳定 artifact_key；只接受业务字段，不接受自定义 key。"""
    if module == "brand":
        return f"brand:{_normalize(brand or '')}"
    if module == "campaign":
        return f"campaign:{_normalize(brand or '')}:{_normalize(campaign or '')}"
    if module == "kol-selection":
        return f"kol-selection:{_stable_hash(_normalize_value(scope))}"
    if module == "kol-analysis":
        return f"kol-analysis:{selection_artifact_id}"
    if module == "kol-detail":
        return f"kol-detail:{platform}:{kol_uid}"
    if module == "insight":
        return (
            f"insight:{parent_artifact_version_id}:"
            f"{_stable_hash(_normalize(question or ''))}"
        )
    raise ValueError(f"unknown artifact module: {module!r}")


__all__ = ["build_artifact_key"]
