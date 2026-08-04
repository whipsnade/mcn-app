"""builders 共享结构：Draft 构建结果与通用 payload 片段（Task 16）。

:class:`DraftBuildResult` 是一次 builder 输出的完整 Draft 描述：Draft 工具
（``agent_runtime/tools/artifacts.py``）据此调用 :class:`ArtifactService`
（Task 12）持久化。builder 只产出，不落库。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class DraftBuildError(ValueError):
    """builder 无法产出合法 Draft（数据不足或与 Schema 契约不符）。"""


@dataclass(frozen=True)
class DraftBuildResult:
    """一次构建的结果：Draft 工具所需的一切字段。

    - ``module``：build_artifact_key 的 key 模块（``kol-selection`` / ``kol-analysis``）；
    - ``schema_version`` / ``artifact_type``：Artifact 的强类型版本；
    - ``business_fields``：生成稳定 artifact_key 的业务字段；
    - ``payload`` / ``evidence_refs``：经 Schema 与 lineage 校验的 Draft 内容；
    - ``parent_artifact_id`` / ``parent_artifact_version_id``：子 Artifact 固定到
      父 Artifact / 父 Version（分析固定到当时的名单 Version）。
    - ``rank_kols_call_id``：评分派生引用的 settled rank_kols 调用（仅名单 builder）。
    """

    module: str
    schema_version: str
    artifact_type: str
    business_fields: dict[str, Any]
    payload: dict[str, Any]
    evidence_refs: list[dict[str, Any]]
    parent_artifact_id: str | None = None
    parent_artifact_version_id: str | None = None
    rank_kols_call_id: str | None = None


def utcnow() -> datetime:
    """当前 UTC 时间（payload 内以 ISO 字符串落库，保持 JSON 可序列化）。"""
    return datetime.now(UTC)


def methodology_dict(
    *,
    data_as_of: datetime | None,
    source_names: tuple[str, ...],
    notes: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "data_as_of": (data_as_of or utcnow()).isoformat(),
        "source_names": list(source_names),
        "notes": list(notes),
    }


def distribution(values: list[Any]) -> list[dict[str, Any]]:
    """``{key, label, count, share}`` 分布统计；None 值跳过，share 按有效计数归一。"""
    counts: dict[Any, int] = {}
    for value in values:
        if value is None:
            continue
        counts[value] = counts.get(value, 0) + 1
    total = sum(counts.values())
    if not total:
        return []
    return [
        {"key": key, "label": key, "count": count, "share": round(count / total, 4)}
        for key, count in sorted(counts.items())
    ]


class LineageCollector:
    """字段级 lineage 收集器：``artifact_path`` → 贡献该值的 Evidence 行集合。

    brand/campaign builder 在聚合时逐字段登记贡献行（``(evidence_id,
    source_path)``），``build()`` 输出 ``evidence_refs_json`` 形状的引用列表
    （``derivation=None``：值由 builder 确定性聚合，来源直接是 Evidence，
    与 kol_detail builder 的既有契约一致）。

    单字段来源超过 ``MAX_SOURCES_PER_REF`` 时按 Evidence 折叠到其最短
    source_path（粒度降级但指针仍可解析），防止大证据集产生超长引用。
    """

    MAX_SOURCES_PER_REF = 12

    def __init__(self) -> None:
        self._refs: dict[str, list[tuple[str, str]]] = {}

    def add(self, artifact_path: str, rows: Iterable[Any]) -> None:
        # 空来源不登记（LineageRef.sources 至少 1 条）；调用方必须用全量行
        # 兜底零值桶。漏登记会由 builder 的必选 numeric 覆盖自检暴露。
        sources = list(rows)
        if not sources:
            return
        bucket = self._refs.setdefault(artifact_path, [])
        for row in sources:
            source = (row.evidence_id, row.source_path)
            if source not in bucket:
                bucket.append(source)

    def build(self) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for artifact_path in sorted(self._refs):
            sources = self._refs[artifact_path]
            if len(sources) > self.MAX_SOURCES_PER_REF:
                shortest: dict[str, str] = {}
                for evidence_id, source_path in sources:
                    current = shortest.get(evidence_id)
                    if current is None or len(source_path) < len(current):
                        shortest[evidence_id] = source_path
                sources = sorted(shortest.items())
            refs.append(
                {
                    "artifact_path": artifact_path,
                    "sources": [
                        {
                            "source_type": "evidence",
                            "evidence_id": evidence_id,
                            "source_path": source_path,
                        }
                        for evidence_id, source_path in sources
                    ],
                    "derivation": None,
                }
            )
        return refs
