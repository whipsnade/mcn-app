"""Deterministic aggregation of the safe BI fields produced by MCP normalizers.

This module deliberately accepts only the projected ``analytics_fields`` mapping
from :mod:`app.reporting.normalizers`.  It never inspects or stores an MCP
payload, URL, credential, or internal call identifier.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


_SENTIMENT_LABELS = {
    "positive": "正向",
    "neutral": "中立",
    "negative": "负向",
}
_SENTIMENT_ORDER = ("positive", "neutral", "negative")
_TOP_HOT_WORDS = 10
_TOP_REGIONS = 5
def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _rounded(value: float) -> int | float:
    rounded = round(value, 2)
    return int(rounded) if rounded.is_integer() else rounded


def _coverage(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _metric(
    value: Any,
    *,
    unit: str,
    covered: int,
    total: int,
    source_fields: Iterable[str],
    platforms: Iterable[str],
) -> dict[str, Any]:
    available = value is not None and covered > 0
    return {
        "value": value if available else None,
        "unit": unit,
        "available": available,
        "coverage": _coverage(covered, total),
        "source_fields": sorted(set(source_fields)) if available else [],
        "platforms": sorted(set(platforms)) if available else [],
    }


def _empty_distribution() -> dict[str, Any]:
    return {
        "value": None,
        "unit": "%",
        "available": False,
        "coverage": 0.0,
        "source_fields": [],
        "platforms": [],
        "items": [],
    }


def empty_analytics() -> dict[str, Any]:
    """Return a stable, explicit empty DTO for old or unavailable reports."""

    return {
        "overview": {
            "brand_volume": _metric(None, unit="条", covered=0, total=0, source_fields=(), platforms=()),
            "total_exposure": _metric(None, unit="次", covered=0, total=0, source_fields=(), platforms=()),
            "average_engagement_rate": _metric(
                None, unit="%", covered=0, total=0, source_fields=(), platforms=()
            ),
        },
        "sentiment": {
            "available": False,
            "coverage": 0.0,
            "source_fields": [],
            "platforms": [],
            "items": [],
            "hot_words": [],
        },
        "exposure_trend": [],
        "audience": {
            "age": _empty_distribution(),
            "gender": _empty_distribution(),
            "regions": _empty_distribution(),
        },
    }


def _record_parts(record: Any) -> tuple[str, str, Mapping[str, Any]]:
    if isinstance(record, Mapping):
        platform = record.get("platform", "")
        identity = record.get("platform_account_id", record.get("account_id", ""))
        fields = record.get("analytics_fields", record)
    else:
        platform = getattr(record, "platform", "")
        identity = getattr(record, "platform_account_id", "")
        fields = getattr(record, "analytics_fields", {})
    if not isinstance(fields, Mapping):
        fields = {}
    return str(platform or ""), str(identity or ""), fields


def _record_key(platform: str, identity: str, fields: Mapping[str, Any]) -> str:
    # Exact duplicate MCP evidence must not double-count.  The digest contains
    # only safe projected fields; no source IDs or raw payload enter it.
    encoded = json.dumps(dict(fields), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"{platform}\x00{identity}\x00{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _iter_records(records: Iterable[Any]) -> list[tuple[str, Mapping[str, Any]]]:
    unique: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for record in records:
        platform, identity, fields = _record_parts(record)
        key = _record_key(platform, identity, fields)
        unique.setdefault(key, (platform, fields))
    return [unique[key] for key in sorted(unique)]


def _field_values(
    records: list[tuple[str, Mapping[str, Any]]], field: str
) -> list[tuple[str, Any]]:
    return [
        (platform, fields[field])
        for platform, fields in records
        if field in fields and fields[field] not in (None, "", [], {})
    ]


def _distribution(
    records: list[tuple[str, Mapping[str, Any]]], field: str, *, limit: int | None = None
) -> dict[str, Any]:
    values = _field_values(records, field)
    if not values:
        return _empty_distribution()
    totals: defaultdict[str, float] = defaultdict(float)
    platforms: set[str] = set()
    valid_values = 0
    for platform, value in values:
        if not isinstance(value, Mapping):
            continue
        valid_values += 1
        platforms.add(platform)
        for label, raw in value.items():
            if not isinstance(label, str):
                continue
            number = _number(raw)
            if number is not None and number <= 100:
                totals[label] += number
    if not totals or not valid_values:
        return _empty_distribution()
    items = [
        {"label": label, "value": _rounded(total / valid_values), "unit": "%"}
        for label, total in totals.items()
    ]
    items.sort(key=lambda item: (-float(item["value"]), item["label"]))
    if limit is not None:
        items = items[:limit]
    covered = valid_values
    return {
        "value": None,
        "unit": "%",
        "available": bool(items),
        "coverage": _coverage(covered, len(records)),
        "source_fields": [field],
        "platforms": sorted(platforms),
        "items": items,
    }


def _hot_words(records: list[tuple[str, Mapping[str, Any]]]) -> tuple[list[dict[str, Any]], set[str]]:
    values = _field_values(records, "hot_words")
    counts: defaultdict[str, float] = defaultdict(float)
    platforms: set[str] = set()
    for platform, value in values:
        entries: list[tuple[str, Any]] = []
        if isinstance(value, Mapping):
            entries = [(str(term), count) for term, count in value.items()]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    entries.append((item, 1))
                elif isinstance(item, Mapping):
                    term = item.get("term")
                    if isinstance(term, str):
                        entries.append((term, item.get("count", item.get("frequency", 1))))
        for term, raw_count in entries:
            number = _number(raw_count)
            term = term.strip()
            if number is not None and term:
                counts[term] += number
                platforms.add(platform)
    items = [
        {"term": term, "count": _rounded(count)}
        for term, count in counts.items()
    ]
    items.sort(key=lambda item: (-float(item["count"]), item["term"]))
    return items[:_TOP_HOT_WORDS], platforms


def _sentiment(records: list[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    values = _field_values(records, "sentiment_counts")
    totals = {key: 0.0 for key in _SENTIMENT_ORDER}
    platforms: set[str] = set()
    covered = 0
    for platform, value in values:
        if not isinstance(value, Mapping):
            continue
        found = False
        for key in _SENTIMENT_ORDER:
            number = _number(value.get(key))
            if number is not None:
                totals[key] += number
                found = True
        if found:
            covered += 1
            platforms.add(platform)
    grand_total = sum(totals.values())
    if not grand_total or not covered:
        result = empty_analytics()["sentiment"]
        hot_words, _ = _hot_words(records)
        result["hot_words"] = hot_words
        return result
    items = [
        {
            "key": key,
            "label": _SENTIMENT_LABELS[key],
            "value": _rounded(totals[key]),
            "percentage": _rounded(totals[key] * 100 / grand_total),
        }
        for key in _SENTIMENT_ORDER
    ]
    hot_words, hot_platforms = _hot_words(records)
    return {
        "available": True,
        "coverage": _coverage(covered, len(records)),
        "source_fields": ["sentiment_counts"],
        "platforms": sorted(platforms | hot_platforms),
        "items": items,
        "hot_words": hot_words,
    }


def _exposure_trend(records: list[tuple[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    totals: defaultdict[str, float] = defaultdict(float)
    platforms: defaultdict[str, set[str]] = defaultdict(set)
    for platform, fields in records:
        exposure = _number(fields.get("exposure"))
        published = fields.get("published_at")
        if exposure is None or published in (None, "", []):
            continue
        dates = published if isinstance(published, list) else [published]
        dates = [str(item)[:10] for item in dates if str(item)[:10]]
        if not dates:
            continue
        share = exposure / len(dates)
        for date in dates:
            totals[date] += share
            platforms[date].add(platform)
    return [
        {
            "date": date,
            "value": _rounded(totals[date]),
            "unit": "次",
            "platforms": sorted(platforms[date]),
        }
        for date in sorted(totals)
    ]


def aggregate_analytics(records: Iterable[Any]) -> dict[str, Any]:
    """Aggregate one completed task's normalized candidate evidence.

    Values are computed only when the corresponding safe field is present.  A
    missing field produces an explicit unavailable metric rather than a zero.
    """

    normalized = _iter_records(records)
    total = len(normalized)

    def sum_metric(field: str, unit: str) -> dict[str, Any]:
        values = _field_values(normalized, field)
        usable = [(platform, _number(value)) for platform, value in values]
        usable = [(platform, number) for platform, number in usable if number is not None]
        return _metric(
            _rounded(sum(number for _platform, number in usable)) if usable else None,
            unit=unit,
            covered=len(usable),
            total=total,
            source_fields=(field,),
            platforms=(platform for platform, _number in usable),
        )

    paired_engagement = []
    for platform, fields in normalized:
        exposure = _number(fields.get("exposure"))
        interactions = _number(fields.get("interactions"))
        if exposure is not None and interactions is not None:
            paired_engagement.append((platform, exposure, interactions))
    exposure_sum = sum(exposure for _platform, exposure, _interactions in paired_engagement)
    interaction_sum = sum(interactions for _platform, _exposure, interactions in paired_engagement)
    engagement = (
        _rounded(interaction_sum * 100 / exposure_sum)
        if exposure_sum > 0 and paired_engagement
        else None
    )
    engagement_platforms = {platform for platform, _exposure, _interactions in paired_engagement}
    engagement_metric = _metric(
        engagement,
        unit="%",
        covered=len(paired_engagement),
        total=total,
        source_fields=("interactions", "exposure"),
        platforms=engagement_platforms,
    )

    return {
        "overview": {
            "brand_volume": sum_metric("brand_mentions", "条"),
            "total_exposure": sum_metric("exposure", "次"),
            "average_engagement_rate": engagement_metric,
        },
        "sentiment": _sentiment(normalized),
        "exposure_trend": _exposure_trend(normalized),
        "audience": {
            "age": _distribution(normalized, "audience_age"),
            "gender": _distribution(normalized, "audience_gender"),
            "regions": _distribution(normalized, "audience_regions", limit=_TOP_REGIONS),
        },
    }


__all__ = ["aggregate_analytics", "empty_analytics"]
