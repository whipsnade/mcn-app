"""BI 报表必需数据项清单与 agent 循环的覆盖判定。

报告由「数据看板 + KOL 看板」两节构成，每节所需数据项在这里集中定义。
agent 循环把清单注入模型上下文（``AgentLoopContext.required_metrics``），
并在模型 finish 前做服务端覆盖校验：某项工具调用 settled 但返回空数据
记为 ``attempted_empty``（视为已尽力，不阻塞 finish）；failed 不覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，避免运行时循环依赖
    from app.orchestration.loop import AgentTrajectory


@dataclass(frozen=True)
class MetricDef:
    """一个必需数据项：key、中文名、给模型看的描述与来源工具。"""

    key: str
    label: str
    description: str
    source_tools: tuple[str, ...]


_ANALYSIS_TOOL = "datatap.insight.query.analysis.v1"
_OVERVIEW_TOOL = "datatap.insight.social.statistic.overview.v1"

BI_REQUIRED_METRICS: tuple[MetricDef, ...] = (
    MetricDef(
        key="brand_voice",
        label="全网品牌声量",
        description="品牌/关键词在统计周期内的全网总声量（提及量）。",
        source_tools=(_ANALYSIS_TOOL, _OVERVIEW_TOOL),
    ),
    MetricDef(
        key="exposure",
        label="总曝光量",
        description="品牌/关键词相关内容在统计周期内的总曝光（阅读/播放）量。",
        source_tools=(_OVERVIEW_TOOL, _ANALYSIS_TOOL),
    ),
    MetricDef(
        key="engagement",
        label="互动量与互动率",
        description="点赞、评论、转发等互动总量及互动率。",
        source_tools=(_ANALYSIS_TOOL,),
    ),
    MetricDef(
        key="sentiment",
        label="情感极性",
        description="正面/中性/负面声量的占比分布。",
        source_tools=(_ANALYSIS_TOOL,),
    ),
    MetricDef(
        key="hot_words",
        label="评论高热词",
        description="统计周期内与品牌相关的热门话题词与评论高热词。",
        source_tools=(
            "datatap.insight.social.statistic.hot.topic.v1",
            "datatap.social.grow.kol.detail.v1",
        ),
    ),
    MetricDef(
        key="voice_trend",
        label="声量/曝光走势",
        description="按天粒度的声量与曝光走势。",
        source_tools=("datatap.insight.social.statistic.trend.v1",),
    ),
    MetricDef(
        key="audience_profile",
        label="受众画像",
        description="受众年龄、性别与省份 Top5 分布。",
        source_tools=("datatap.insight.social.statistic.user.profile.v1",),
    ),
    MetricDef(
        key="kol_leaderboard",
        label="达人绩效明细",
        description=(
            "达人名称、层级、粉丝量、渠道、互动率、声量贡献与正向舆情"
            "（不含投放成本）。"
        ),
        source_tools=(
            "datatap.insight.social.statistic.hot.user.v1",
            "datatap.insight.query.rank.list.v1",
            "datatap.xiaohongshu.kol.search.v1",
            "datatap.douyin.kol.search.v1",
            "datatap.social.grow.kol.bilibili.search.v1",
            "datatap.social.grow.kol.weibo.search.v1",
            "datatap.social.grow.kol.wechat.search.v1",
            "datatap.social.grow.kol.detail.v1",
        ),
    ),
)

def _metrics_by_tool(metrics: Iterable[MetricDef]) -> dict[str, tuple[MetricDef, ...]]:
    by_tool: dict[str, list[MetricDef]] = {}
    for metric in metrics:
        for tool in metric.source_tools:
            by_tool.setdefault(tool, []).append(metric)
    return {tool: tuple(items) for tool, items in by_tool.items()}


_METRICS_BY_TOOL = _metrics_by_tool(BI_REQUIRED_METRICS)


@dataclass(frozen=True)
class MetricCoverage:
    """覆盖结果：covered 为有数据支撑的项，attempted_empty 为已尽力但返回空的项。"""

    covered: frozenset[str]
    attempted_empty: frozenset[str]


def _is_empty_summary(summary: Any) -> bool:
    """空值判定：None、空 dict/list，或 JSON 编码后为 null/{}/[] 的字符串。"""
    if summary is None:
        return True
    if isinstance(summary, (dict, list)) and not summary:
        return True
    if isinstance(summary, str):
        text = summary.strip()
        if not text:
            return True
        try:
            decoded = json.loads(text)
        except ValueError:
            return False
        return decoded is None or decoded == {} or decoded == []
    return False


def metric_coverage(
    trajectory: AgentTrajectory, metrics: Iterable[MetricDef] | None = None
) -> MetricCoverage:
    """按轨迹中 settled 的工具调用映射数据项覆盖情况。

    failed 调用不产生任何覆盖；settled 但 summary 为空的调用记入
    ``attempted_empty``；同一数据项只要有任一有数据的 settled 调用即
    视为 covered（覆盖优先级高于 attempted_empty）。``metrics`` 缺省为
    全局 ``BI_REQUIRED_METRICS``；executor 以注入上下文的清单为准。
    """
    by_tool = _METRICS_BY_TOOL if metrics is None else _metrics_by_tool(metrics)
    covered: set[str] = set()
    attempted_empty: set[str] = set()
    for note in trajectory.results:
        if note.status != "settled":
            continue
        mapped = by_tool.get(note.tool, ())
        if not mapped:
            continue
        if _is_empty_summary(note.summary):
            attempted_empty.update(metric.key for metric in mapped)
        else:
            covered.update(metric.key for metric in mapped)
    return MetricCoverage(
        covered=frozenset(covered),
        attempted_empty=frozenset(attempted_empty - covered),
    )


def missing_metrics(
    coverage: MetricCoverage, metrics: Iterable[MetricDef] | None = None
) -> list[MetricDef]:
    """未覆盖且未 attempted_empty 的数据项，按清单顺序返回。"""
    satisfied = coverage.covered | coverage.attempted_empty
    checklist = BI_REQUIRED_METRICS if metrics is None else tuple(metrics)
    return [metric for metric in checklist if metric.key not in satisfied]


def required_metrics_payload() -> tuple[dict[str, Any], ...]:
    """注入 AgentLoopContext.required_metrics 的序列化清单。"""
    return tuple(
        {
            "key": metric.key,
            "label": metric.label,
            "description": metric.description,
            "source_tools": list(metric.source_tools),
        }
        for metric in BI_REQUIRED_METRICS
    )


__all__ = [
    "BI_REQUIRED_METRICS",
    "MetricCoverage",
    "MetricDef",
    "metric_coverage",
    "missing_metrics",
    "required_metrics_payload",
]
