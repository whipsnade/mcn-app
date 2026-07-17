from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.orchestration.schemas import AnalysisScope, SessionBrief


_BRAND_TERMS = (
    "声量",
    "舆情",
    "情感",
    "情绪",
    "趋势",
    "热词",
    "品牌提及",
    "曝光",
    "口碑",
    "讨论",
    "品牌分析",
)
_KOL_TERMS = (
    "达人",
    "kol",
    "网红",
    "粉丝",
    "候选",
    "活跃达人",
    "博主",
    "账号",
)
_BRAND_ONLY_TERMS = ("仅分析品牌", "只分析品牌", "仅品牌维度", "不需要达人", "不找达人")
_PERIOD_PATTERN = re.compile(r"(?:最近|近)(?P<value>\d+)\s*(?P<unit>天|日|个月|月|季度|年)")


class AnalysisRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: AnalysisScope
    objectives: tuple[str, ...] = Field(default_factory=tuple, max_length=12)
    requested_period: dict[str, Any] = Field(default_factory=dict)


def _period_from_text(text: str) -> dict[str, Any]:
    match = _PERIOD_PATTERN.search(text)
    if match is None:
        value, unit = 30, "day"
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


def classify_analysis_request(text: str, brief: SessionBrief) -> AnalysisRouting:
    content = f"{brief.brand}\n{brief.category}\n{text}".casefold()
    has_brand = any(term.casefold() in content for term in _BRAND_TERMS)
    has_kol = any(term.casefold() in content for term in _KOL_TERMS)
    explicit_brand_only = any(term in content for term in _BRAND_ONLY_TERMS)
    if explicit_brand_only:
        # “不需要达人” contains the KOL keyword itself but is a negative
        # instruction, not a request for creator discovery.
        has_kol = False
    if has_brand and (has_kol or not explicit_brand_only):
        scope: AnalysisScope = "hybrid"
    elif has_brand:
        scope = "brand"
    else:
        scope = "kol"

    objectives: list[str] = []
    if has_brand:
        objectives.append("brand_analysis")
    if any(term in content for term in ("声量", "提及", "讨论", "曝光")):
        objectives.append("volume_trend")
    if any(term in content for term in ("情感", "情绪", "舆情", "口碑")):
        objectives.append("sentiment_trend")
    if any(term in content for term in ("热词", "话题")):
        objectives.append("hot_topics")
    if any(term in content for term in ("画像", "人群", "地域", "性别", "年龄")):
        objectives.append("audience_profile")
    if has_kol or scope == "hybrid":
        objectives.append("kol_discovery")
    if any(term in content for term in ("对比", "比较", "各平台")):
        objectives.append("platform_comparison")
    if not objectives:
        objectives.append("kol_discovery")
    return AnalysisRouting(
        scope=scope,
        objectives=tuple(dict.fromkeys(objectives)),
        requested_period=_period_from_text(text),
    )


__all__ = ["AnalysisRouting", "classify_analysis_request"]
