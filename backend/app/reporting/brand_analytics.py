from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from app.reporting.analytics import aggregate_analytics, empty_analytics


def _parts(record: Any) -> tuple[str, str, Mapping[str, Any]]:
    if isinstance(record, Mapping):
        platform = str(record.get("platform") or "all")
        period = str(record.get("period") or "")
        fields = record.get("analytics_fields", {})
    else:
        platform = str(getattr(record, "platform", "all") or "all")
        period = str(getattr(record, "period", "") or "")
        fields = getattr(record, "analytics_fields", {})
    return platform, period, fields if isinstance(fields, Mapping) else {}


def _trend(
    records: Iterable[Any], field: str, *, unit: str, average: bool = False
) -> list[dict[str, Any]]:
    totals: defaultdict[str, float] = defaultdict(float)
    counts: defaultdict[str, int] = defaultdict(int)
    platforms: defaultdict[str, set[str]] = defaultdict(set)
    for record in records:
        platform, period, fields = _parts(record)
        if not period:
            value = fields.get("published_at")
            period = str(value or "")
        raw = fields.get(field)
        if not period or not isinstance(raw, (int, float)) or isinstance(raw, bool):
            continue
        totals[period] += float(raw)
        counts[period] += 1
        platforms[period].add(platform)
    result = []
    for period in sorted(totals):
        value = totals[period] / counts[period] if average else totals[period]
        rounded = round(value, 4)
        if float(rounded).is_integer():
            rounded = int(rounded)
        result.append(
            {
                "period": period,
                "value": rounded,
                "unit": unit,
                "platforms": sorted(platforms[period]),
            }
        )
    return result


def aggregate_brand_analytics(records: Iterable[Any]) -> dict[str, Any]:
    normalized = tuple(records)
    base_records = []
    for record in normalized:
        platform, _period, fields = _parts(record)
        base_records.append(
            {
                "platform": platform,
                "platform_account_id": "brand",
                "analytics_fields": dict(fields),
            }
        )
    result = aggregate_analytics(base_records) if base_records else empty_analytics()
    result["volume_trend"] = _trend(normalized, "brand_mentions", unit="条")
    result["sentiment_trend"] = _trend(
        normalized, "sentiment_index", unit="指数", average=True
    )
    available_fields = sorted(
        {
            field
            for _platform, _period, fields in (_parts(record) for record in normalized)
            for field in fields
        }
    )
    result["data_availability"] = {
        "available": bool(available_fields),
        "record_count": len(normalized),
        "fields": available_fields,
    }
    result["warnings"] = [] if available_fields else ["暂无真实 MCP 品牌数据"]
    return result


__all__ = ["aggregate_brand_analytics"]
