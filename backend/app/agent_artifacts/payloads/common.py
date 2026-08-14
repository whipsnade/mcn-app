"""Shared outer shell and reusable building blocks for the typed artifact payloads.

All five strongly-typed payloads (§12.1) and the generic `insight_board_v1`
(§12.2) share this shell: a fixed `schema_version`, a `module`, a `data_status`
aggregated over required sections, per-section `availability`, disclosed
`limitations`, and a `methodology`.

Every payload is a frozen Pydantic contract with `extra="forbid"`:
- `data_status` must match the required-section availability aggregate.
- A business numeric that is `None` is allowed only when its owning section is
  `partial`/`unavailable` and a limitation covers it; it is never coerced to 0.
- `narrative` may only cite `data` via `supporting_paths`; each path must
  resolve inside `data`.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    Union,
    get_args,
    get_origin,
)
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

_MISSING = object()
_NONE_TYPE = type(None)


def _check_url(value: str | None) -> str | None:
    """Enforce http/https only for URL fields; allow None (missing disclosure)."""
    if value is None:
        return value
    try:
        scheme = urlsplit(value).scheme
    except ValueError as exc:  # pragma: no cover - urlsplit is permissive
        raise ValueError(f"invalid url: {value!r}") from exc
    if scheme not in ("http", "https"):
        raise ValueError(f"url must use http or https, got scheme {scheme!r}")
    return value


HttpUrl = Annotated[str, AfterValidator(_check_url)]
OptionalHttpUrl = Annotated[str | None, AfterValidator(_check_url)]


class Period(BaseModel):
    """Query period common to brand / campaign / insight scopes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: date
    end: date
    timezone: str


class SectionAvailability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["complete", "partial", "unavailable"]
    reason_codes: tuple[str, ...] = ()


class Limitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    affected_paths: tuple[str, ...] = ()


class Methodology(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data_as_of: datetime
    source_names: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class DistributionItem(BaseModel):
    """{key, label, count, share} used by kol_selection summary and kol_analysis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    count: int
    share: float


class ContentTypeItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    type: str
    posts: int | None
    volume: int | None
    engagement: int | None


class SentimentBucket(BaseModel):
    """情感桶（§6.3）：count/share 可空。

    情感数据缺失时唯一合法表达是 null + 章节 partial/unavailable + 覆盖
    limitation，不得伪造 0；真实零值只在 Evidence 明确返回 0 时写入，
    作为数值叶子需要 lineage。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int | None
    share: float | None


class SentimentSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    positive: SentimentBucket
    neutral: SentimentBucket
    negative: SentimentBucket


class SentimentByPlatform(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    positive: SentimentBucket
    neutral: SentimentBucket
    negative: SentimentBucket


class SentimentSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: SentimentSummary
    by_platform: tuple[SentimentByPlatform, ...] = ()


class TopPost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    post_id: str
    # title/author 缺失时显式 None（不伪造空字符串，与 canonical unavailable 一致）。
    title: str | None
    url: OptionalHttpUrl
    author: str | None
    published_at: datetime
    likes: int | None
    comments: int | None
    shares: int | None
    engagement: int | None


class NarrativeFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    detail: str
    supporting_paths: tuple[str, ...] = ()


class NarrativeRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    action: str
    rationale: str
    supporting_paths: tuple[str, ...] = ()


def _resolve_path(node: Any, path: str) -> Any:
    """Resolve a dotted path against a dict/list tree; `_MISSING` if absent."""
    current = node
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, (list, tuple)):
            if not part.isdigit() or int(part) >= len(current):
                return _MISSING
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _to_dict(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_dict(item) for item in value]
    return value


def _collect_supporting_paths(narrative: BaseModel) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            paths = node.get("supporting_paths")
            if isinstance(paths, (list, tuple)):
                found.extend(p for p in paths if isinstance(p, str))
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(_to_dict(narrative))
    return found


def _has_covering_limitation(limitations: tuple[Limitation, ...], path: str) -> bool:
    """A limitation either pins the exact data path or is generic (empty paths)."""
    full = f"data.{path}"
    return any(
        not limitation.affected_paths
        or full in limitation.affected_paths
        or path in limitation.affected_paths
        for limitation in limitations
    )


def _has_section_limitation(limitations: tuple[Limitation, ...], section: str) -> bool:
    """A limitation covers a section: generic (empty paths) or its first path
    segment (after an optional leading ``data``) equals the section name."""
    for limitation in limitations:
        if not limitation.affected_paths:
            return True
        for path in limitation.affected_paths:
            parts = path.split(".")
            if parts and parts[0] == "data":
                parts = parts[1:]
            if parts and parts[0] == section:
                return True
    return False


def _is_optional_numeric(annotation: Any) -> tuple[bool, Any]:
    """``(True, None)`` if the annotation is exactly ``int | None`` / ``float | None``
    (bool excluded); otherwise ``(False, inner)`` with ``inner`` the single
    non-None member of an Optional union, or None when not an Optional union."""
    origin = get_origin(annotation)
    if origin is not Union and origin is not types.UnionType:
        return False, None
    args = get_args(annotation)
    if _NONE_TYPE not in args:
        return False, None
    non_none = tuple(arg for arg in args if arg is not _NONE_TYPE)
    if len(non_none) != 1:
        return False, None
    inner = non_none[0]
    if inner is int or inner is float:
        return True, None
    return False, inner


def iter_null_numeric_paths(annotation: Any, value: Any, path: str) -> Iterator[str]:
    """Yield dotted paths of every None Optional-numeric leaf under ``value``.

    ``annotation`` is the Pydantic field annotation describing ``value``; the
    walk recurses through nested models, arrays (indexed path segments) and
    dict values. Only leaves typed exactly ``int | None`` / ``float | None``
    are governed — dates, enums, stable identities, versions, display order,
    plain-text labels and runtime metadata are exempt (§12.1/§6.3), as are
    non-optional numerics (they can never be None by contract).
    """
    is_numeric, inner = _is_optional_numeric(annotation)
    if is_numeric:
        if value is None:
            yield path
        return
    if inner is not None:
        # Optional 非数值叶子（OptionalHttpUrl、Period | None 等）：豁免；
        # 非 None 的可空嵌套模型继续向下遍历。
        if value is not None:
            yield from iter_null_numeric_paths(inner, value, path)
        return
    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        args = get_args(annotation)
        if args and value:
            for index, item in enumerate(value):
                yield from iter_null_numeric_paths(args[0], item, f"{path}.{index}")
        return
    if origin is dict:
        args = get_args(annotation)
        if len(args) == 2 and value:
            for key, item in value.items():
                yield from iter_null_numeric_paths(args[1], item, f"{path}.{key}")
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        for name, field in annotation.model_fields.items():
            yield from iter_null_numeric_paths(
                field.annotation, getattr(value, name, None), f"{path}.{name}"
            )


def validate_unique(items: Sequence[Any], key_fields: Sequence[str], label: str) -> None:
    """Reject duplicate stable-business keys within a sequence of items.

    Array items carry stable business keys (spec §12.1); duplicates are schema
    errors. With empty `key_fields` the item itself is the key (e.g. column
    headers of an insight table).
    """
    seen: set[Any] = set()
    for item in items:
        key: Any = item if not key_fields else tuple(getattr(item, field) for field in key_fields)
        if key in seen:
            raise ValueError(f"duplicate stable key {key!r} in {label}")
        seen.add(key)


class UniqueKeyValidator(BaseModel):
    """Mixin enforcing stable-business-key uniqueness across tuple fields.

    Subclasses declare `STABLE_KEYS: dict[field_name, tuple[key_field, ...]]`;
    every tuple field listed is checked for duplicates after validation.
    """

    STABLE_KEYS: ClassVar[dict[str, tuple[str, ...]]] = {}

    @model_validator(mode="after")
    def _validate_stable_keys(self) -> UniqueKeyValidator:
        for field_name, key_fields in self.STABLE_KEYS.items():
            validate_unique(getattr(self, field_name), key_fields, field_name)
        return self


class ArtifactPayloadBase(BaseModel):
    """Shared frozen shell for all typed payloads.

    Concrete subclasses declare their own `scope`, `data` and `narrative`
    models, plus class metadata:
    - `REQUIRED_SECTIONS`: sections that must be complete for data_status=complete.
    - `GOVERNED_SECTIONS`: business section roots (same-named top-level `data`
      fields) whose Optional numeric leaves are null-governed (§6.3). The
      validator derives the leaves recursively from the Pydantic schema —
      array elements included — instead of a hand-maintained path list.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    module: Literal["brand", "campaign", "kol"]
    data_status: Literal["complete", "restricted"]
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...]
    methodology: Methodology

    REQUIRED_SECTIONS: ClassVar[frozenset[str]] = frozenset()
    GOVERNED_SECTIONS: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _validate_aggregate_and_limitations(self) -> ArtifactPayloadBase:
        availability = self.availability
        required = self.REQUIRED_SECTIONS
        data_dict = _to_dict(self.data)

        # §2.5 反向聚合：所有必需章节必须存在于 availability。
        missing_required = sorted(section for section in required if section not in availability)
        if missing_required:
            raise ValueError(
                f"availability is missing required sections: {missing_required}"
            )

        if self.data_status == "complete":
            for section in required:
                entry = availability[section]
                if entry.status != "complete":
                    raise ValueError(
                        f"data_status=complete requires required section {section!r} to be complete"
                    )
        elif self.data_status == "restricted":
            # §2.5 反向聚合：restricted 当且仅当至少一个必需章节受限，且有覆盖
            # 该章节的 limitation（affected_paths 为空 = 通用覆盖）。
            restricted_required = [
                section for section in required if availability[section].status != "complete"
            ]
            if not restricted_required:
                raise ValueError(
                    "data_status=restricted requires at least one required section "
                    "to be partial/unavailable"
                )
            if not any(
                _has_section_limitation(self.limitations, section)
                for section in restricted_required
            ):
                raise ValueError(
                    "data_status=restricted requires a limitation covering a "
                    "restricted required section"
                )

        # null business numeric is allowed only when its section is partial/unavailable
        # AND a limitation covers it (generic, section-level, or the exact leaf
        # path); the null must never be coerced to 0. Leaves are derived
        # recursively from the schema — array elements included (§6.3).
        data_model = type(self.data)
        if self.GOVERNED_SECTIONS and issubclass(data_model, BaseModel):
            for section in sorted(self.GOVERNED_SECTIONS):
                field = data_model.model_fields.get(section)
                if field is None:
                    continue  # 声明漂移由契约测试拦截（GOVERNED_SECTIONS ⊆ data 字段）
                entry = availability.get(section)
                for path in iter_null_numeric_paths(
                    field.annotation, getattr(self.data, section), section
                ):
                    if entry is None or entry.status == "complete":
                        raise ValueError(
                            f"null value at data.{path} requires section {section!r} "
                            "to be partial/unavailable"
                        )
                    if not (
                        _has_covering_limitation(self.limitations, path)
                        or _has_section_limitation(self.limitations, section)
                    ):
                        raise ValueError(
                            f"null value at data.{path} requires a covering limitation"
                        )

        # narrative may only cite data via supporting_paths; each must resolve.
        for supporting_path in _collect_supporting_paths(self.narrative):
            path = supporting_path.removeprefix("data.")
            if _resolve_path(data_dict, path) is _MISSING:
                raise ValueError(
                    f"narrative supporting_path {supporting_path!r} does not resolve in data"
                )
        return self
