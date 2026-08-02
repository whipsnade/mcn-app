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

from datetime import date, datetime
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

_MISSING = object()


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
    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int
    share: float


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
    title: str
    url: OptionalHttpUrl
    author: str
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


class ArtifactPayloadBase(BaseModel):
    """Shared frozen shell for all typed payloads.

    Concrete subclasses declare their own `scope`, `data` and `narrative`
    models, plus class metadata:
    - `REQUIRED_SECTIONS`: sections that must be complete for data_status=complete.
    - `SECTION_NUMERIC_PATHS`: section name -> data paths whose None-ness is
      governed by that section's availability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    module: Literal["brand", "campaign", "kol"]
    data_status: Literal["complete", "restricted"]
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...]
    methodology: Methodology

    REQUIRED_SECTIONS: ClassVar[frozenset[str]] = frozenset()
    SECTION_NUMERIC_PATHS: ClassVar[dict[str, tuple[str, ...]]] = {}

    @model_validator(mode="after")
    def _validate_aggregate_and_limitations(self) -> ArtifactPayloadBase:
        availability = self.availability
        required = self.REQUIRED_SECTIONS
        data_dict = _to_dict(self.data)

        if self.data_status == "complete":
            for section in required:
                entry = availability.get(section)
                if entry is None or entry.status != "complete":
                    raise ValueError(
                        f"data_status=complete requires required section {section!r} to be complete"
                    )
        elif self.data_status == "restricted":
            if not self.limitations:
                raise ValueError("data_status=restricted requires at least one limitation")

        # null business numeric is allowed only when its section is partial/unavailable
        # AND a limitation covers it; the null must never be coerced to 0.
        for section, paths in self.SECTION_NUMERIC_PATHS.items():
            entry = availability.get(section)
            for path in paths:
                if _resolve_path(data_dict, path) is not None:
                    continue
                if entry is None or entry.status == "complete":
                    raise ValueError(
                        f"null value at data.{path} requires section {section!r} "
                        "to be partial/unavailable"
                    )
                if not _has_covering_limitation(self.limitations, path):
                    raise ValueError(f"null value at data.{path} requires a covering limitation")

        # narrative may only cite data via supporting_paths; each must resolve.
        for supporting_path in _collect_supporting_paths(self.narrative):
            path = supporting_path.removeprefix("data.")
            if _resolve_path(data_dict, path) is _MISSING:
                raise ValueError(
                    f"narrative supporting_path {supporting_path!r} does not resolve in data"
                )
        return self
