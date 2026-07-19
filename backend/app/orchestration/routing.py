from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any


_PERIOD_PATTERN = re.compile(r"(?:最近|近)(?P<value>\d+)\s*(?P<unit>天|日|个月|月|季度|年)")


def _period_from_text(text: str) -> dict[str, Any]:
    match = _PERIOD_PATTERN.search(text)
    if match is None:
        # 未明确时间范围时默认最近三个月。
        value, unit = 3, "month"
    else:
        value = int(match.group("value"))
        raw_unit = match.group("unit")
        unit = "day" if raw_unit in {"天", "日"} else "month" if raw_unit in {"月", "个月"} else raw_unit
        if unit == "季度":
            unit, value = "month", value * 3
        elif unit == "年":
            unit, value = "month", value * 12
    end = date.today()
    start = end - (timedelta(days=value) if unit == "day" else timedelta(days=30 * value))
    return {
        "unit": unit,
        "value": value,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def extract_requested_period(text: str) -> dict[str, Any]:
    """Normalize only the requested time window; never classify the task scope."""
    return _period_from_text(text)


__all__ = [
    "extract_requested_period",
]
