"""对外发布的 canonical 业务字段契约（Task 3R）。

两层结构：
- 内部 normalized source rows（``raw_rows.py``）：统一 DataTap 原始字段别名，
  只供 Builder 计算，不对外发布；
- 对外发布的 :class:`CanonicalField`：表达 Artifact 最终业务数据，是 Task 4
  数值 lineage 门禁的唯一事实源。path 是稳定的 Artifact JSON Pointer
  （``/data/...``），value 必须等于该 Artifact 路径的最终业务值；DataTap 原始
  key 与 source_path 只保留在 Evidence/evidence_refs 层，绝不进入 canonical path。

契约规则（fail-closed）：
- ``path`` 非空且以 ``/data/`` 开头；
- ``availability`` 只能是 complete/partial/unavailable；
- ``availability=unavailable`` 时 ``value`` 必须为 None，反之亦然；
- complete/partial 的数值字段必须至少有一个 evidence_id；
- ``evidence_ids`` 去重（重复即拒绝）且输出顺序稳定；
- 同一 payload 内 canonical path 不允许重复；``field_lineage`` 与 canonical
  path 集合完全一致，且只映射到该最终 canonical field（恒等映射）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

CanonicalAvailability = Literal["complete", "partial", "unavailable"]

# 数值叶子单位（按 path 末段推断；比率/文本字段无单位）。
_UNIT_BY_SUFFIX: dict[str, str] = {
    "total_volume": "mentions",
    "volume": "mentions",
    "total_engagement": "interactions",
    "engagement": "interactions",
    "total_posts": "posts",
    "posts": "posts",
    "likes": "count",
    "comments": "count",
    "shares": "count",
    "collects": "count",
    "creators": "count",
    "creator_count": "count",
    "total_creators": "count",
    "count": "count",
    "conversions": "count",
    "sentiment_score": "score",
    "date": "timestamp",
    "published_at": "timestamp",
    "positive": "mentions",
    "neutral": "mentions",
    "negative": "mentions",
    "spend": "currency",
    "revenue": "currency",
    "impressions": "impressions",
    "cpc": "currency_per_conversion",
    "cpm": "currency_per_thousand_impressions",
    "roi": "ratio",
    "roas": "ratio",
}


def unit_for_path(path: str, *, module: str | None = None, data: dict[str, Any] | None = None) -> str | None:
    """按 module、章节与指标推导单位，不把 Campaign 帖数误标为声量。"""
    suffix = path.rsplit("/", 1)[-1]
    if suffix in {"rate", "share", "share_of_voice", "contribution_share"}:
        return "ratio"
    if suffix in {"current", "baseline", "delta"} and data is not None:
        metric = _metric_for_comparison_path(path, data)
        comparison_units = {
            "total_volume": "mentions" if module == "brand" else "posts",
            "total_engagement": "interactions",
            "total_posts": "posts",
            "volume": "posts" if module == "campaign" else "mentions",
            "engagement": "interactions",
            "posts": "posts",
            "creators": "count",
        }
        if metric in comparison_units:
            return comparison_units[metric]
    if module == "campaign" and suffix in {"total_volume", "volume", "total_posts", "posts"}:
        return "posts"
    return _UNIT_BY_SUFFIX.get(suffix)


def _metric_for_comparison_path(path: str, data: dict[str, Any]) -> str | None:
    """取比较指标同级 ``metric``，路径不完整或不存在时保持无单位。"""
    parts = path.removeprefix("/data/").split("/")[:-1]
    current: Any = data
    try:
        for token in parts:
            current = current[int(token)] if isinstance(current, list) else current[token]
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    metric = current.get("metric") if isinstance(current, dict) else None
    return metric if isinstance(metric, str) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class CanonicalField(BaseModel):
    """表达 Artifact 最终业务数据的可审计字段（Task 4 lineage 唯一事实源）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    value: Any = None
    availability: CanonicalAvailability
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    unit: str | None = None

    @model_validator(mode="after")
    def _validate_canonical_field(self) -> CanonicalField:
        rendered = self.path.strip()
        if not rendered or not rendered.startswith("/data/"):
            raise ValueError(f"canonical path must be non-empty and start with /data/, got {self.path!r}")
        if self.availability == "unavailable":
            if self.value is not None:
                raise ValueError("availability=unavailable requires value=None")
        elif self.value is None:
            raise ValueError(f"canonical field {self.path!r} with value=None must be unavailable")
        elif _is_number(self.value) and not self.evidence_ids:
            raise ValueError(
                f"complete/partial numeric canonical field {self.path!r} requires evidence_ids"
            )
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError(f"canonical field {self.path!r} has duplicate evidence_ids")
        return self


class CanonicalPayloadMixin(BaseModel):
    """Brand/Campaign payload 的 canonical 契约外壳（强类型，不再以 dict 绕过验证）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_data: tuple[CanonicalField, ...] = Field(default_factory=tuple)
    field_lineage: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_canonical_contract(self) -> CanonicalPayloadMixin:
        has_canonical = bool(self.canonical_data)
        has_lineage = bool(self.field_lineage)
        if has_canonical != has_lineage:
            raise ValueError(
                "canonical_data and field_lineage must be both present or both absent"
            )
        # 历史 Artifact 在 Task 3R 前没有这两个字段；读取和导出保持兼容。
        # 新建/更新/发布由 require_canonical() 额外执行严格门禁。
        if not has_canonical:
            return self
        paths = [field.path for field in self.canonical_data]
        if len(set(paths)) != len(paths):
            raise ValueError(f"duplicate canonical path: {sorted({p for p in paths if paths.count(p) > 1})}")
        path_set = set(paths)
        if set(self.field_lineage) != path_set:
            missing = sorted(path_set - set(self.field_lineage))
            extra = sorted(set(self.field_lineage) - path_set)
            raise ValueError(
                "field_lineage keys must exactly match canonical paths"
                + (f"; missing {missing}" if missing else "")
                + (f"; extra {extra}" if extra else "")
            )
        for key, values in self.field_lineage.items():
            if not values:
                raise ValueError(f"field_lineage[{key!r}] must not be empty")
            if tuple(values) != (key,):
                raise ValueError(
                    f"field_lineage[{key!r}] must map only to its own canonical field, got {values!r}"
                )
        data = getattr(self, "data", None)
        if data is None:
            return self
        data_leaves = dict(walk_data_leaves(_json_mode(data)))
        canonical_values = {field.path: _json_mode(field.value) for field in self.canonical_data}
        data_paths = set(data_leaves)
        if set(canonical_values) != data_paths:
            missing = sorted(data_paths - set(canonical_values))
            extra = sorted(set(canonical_values) - data_paths)
            raise ValueError(
                "canonical paths must exactly match final data leaves"
                + (f"; missing {missing}" if missing else "")
                + (f"; extra {extra}" if extra else "")
            )
        for path, value in canonical_values.items():
            if value != data_leaves[path]:
                raise ValueError(f"canonical value does not match final data at {path!r}")
        return self

    def require_canonical(self) -> CanonicalPayloadMixin:
        """要求新建、更新或发布的 payload 携带完整 canonical 合同。"""
        if not self.canonical_data or not self.field_lineage:
            raise ValueError("canonical contract is required for new or published payloads")
        return self


def _json_mode(value: Any) -> Any:
    """以 Pydantic JSON-mode 比较业务值，消除日期与 tuple 的内存形态差异。"""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return TypeAdapter(Any).dump_python(value, mode="json")


def _escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def walk_data_leaves(node: Any, prefix: str = "/data") -> list[tuple[str, Any]]:
    """递归收集 ``data`` 下的全部叶子 ``(path, value)``（数组下标按 RFC 6901 数字段）。"""
    leaves: list[tuple[str, Any]] = []

    def walk(current: Any, parts: list[str]) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                walk(child, [*parts, _escape_token(str(key))])
        elif isinstance(current, (list, tuple)):
            for index, child in enumerate(current):
                walk(child, [*parts, str(index)])
        else:
            leaves.append(("/" + "/".join(parts), current))

    walk(node, ["data"])
    return leaves


def _evidence_ids_by_path(
    refs: list[dict[str, Any]],
) -> dict[str, tuple[str, ...]]:
    """从 LineageCollector 输出提取 ``artifact_path -> 去重保序 Evidence ID``。"""
    index: dict[str, list[str]] = {}
    for ref in refs:
        path = ref.get("artifact_path")
        if not isinstance(path, str):
            continue
        bucket = index.setdefault(path, [])
        for source in ref.get("sources", []):
            evidence_id = source.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id not in bucket:
                bucket.append(evidence_id)
    return {path: tuple(ids) for path, ids in index.items()}


def publish_canonical(
    data: dict[str, Any],
    refs: list[dict[str, Any]],
    partial_paths: frozenset[str] = frozenset(),
    *,
    module: str | None = None,
) -> tuple[list[CanonicalField], dict[str, tuple[str, ...]]]:
    """把最终业务 ``data`` 发布为 canonical 字段 + 恒等 field_lineage。

    - value 直接取自 ``data`` 对应路径（与最终 payload 同一计算结果）；
    - availability：value None → unavailable；``partial_paths`` 命中 → partial；
      其余 complete；
    - evidence_ids 来自 ``refs``（参与计算的全部 Evidence，去重保序）；
    - field_lineage 恒等映射到该最终 canonical field。
    """
    evidence_by_path = _evidence_ids_by_path(refs)
    leaves = sorted(walk_data_leaves(data), key=lambda item: item[0])
    fields: list[CanonicalField] = []
    for path, value in leaves:
        availability = (
            "unavailable"
            if value is None
            else ("partial" if path in partial_paths else "complete")
        )
        fields.append(
            CanonicalField(
                path=path,
                value=value,
                availability=availability,
                evidence_ids=evidence_by_path.get(path, ()),
                unit=unit_for_path(path, module=module, data=data),
            )
        )
    lineage = {field.path: (field.path,) for field in fields}
    return fields, lineage


__all__ = [
    "CanonicalAvailability",
    "CanonicalField",
    "CanonicalPayloadMixin",
    "publish_canonical",
    "unit_for_path",
    "walk_data_leaves",
]
