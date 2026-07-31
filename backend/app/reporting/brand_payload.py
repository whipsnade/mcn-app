"""brand_report_v2 结构化快照契约（品牌报告导出与 BI 重构，Task 4）。

`data` 是唯一数值事实来源：由 `brand_assembler` 从 task.plan_json 的 settled
证据确定性归一得到；对比期数值与环比/同比百分比都由组装器算好，模型叙事
（Task 5）只能引用 `data`，不得创造或换算指标。导出端点与 BI 均可用
`BrandReportPayload.model_validate` 复验落库快照。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


# 7 个数据章节：全部 complete 时整体 data_status 才是 complete。
DATA_CHAPTERS: tuple[str, ...] = (
    "overview",
    "sentiment",
    "daily_trend",
    "content_creators",
    "regions",
    "top_posts",
    "insights",
)
# methodology 由服务端生成，恒 complete，不参与降级。
ALL_CHAPTERS: tuple[str, ...] = DATA_CHAPTERS + ("methodology",)


class PeriodValue(BaseModel):
    """单期数值 + 取得状态。"""

    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    status: Literal["ok", "not_requested", "restricted"] = "ok"
    # restricted 时必填：invalid_period / insufficient_points / no_data / tool_failed
    reason: str | None = None


class ChapterAvailability(BaseModel):
    """单章节可用性：状态、缺失字段、受限原因与来源追溯。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "partial", "unavailable"]
    missing_fields: list[str] = Field(default_factory=list)
    reason: str | None = None
    source_tools: list[str] = Field(default_factory=list)
    # 轨迹（v1/v2）的 EvidenceNote 不携带采集时间戳，组装器产出恒为 null；
    # 源级时间戳需关联 mcp_calls，超出组装器输入范围。
    collected_at: str | None = None


class ReportScope(BaseModel):
    """报告范围快照：品牌、时间窗、平台、对比口径与数据截至日。"""

    model_config = ConfigDict(extra="forbid")

    brand: str = ""
    period_start: str | None = None
    period_end: str | None = None
    platforms: list[str] = Field(default_factory=list)
    comparison_mode: Literal["mom", "mom_yoy"] = "mom"
    # 趋势最大证据日期 < period.end 时写入（数据不完整必须明示截至日）。
    data_as_of: str | None = None


class QuerySpec(BaseModel):
    """查询口径：原始品牌名、标签匹配结果与比较期定义。"""

    model_config = ConfigDict(extra="forbid")

    original_term: str = ""
    matched_tag: str | None = None
    # 标签匹配失败时回退为原始品牌名（keyword 查询）。
    fallback_keyword: str | None = None
    comparison_definition: str = ""


class SourceEntry(BaseModel):
    """一条 settled 工具调用的来源记录。"""

    model_config = ConfigDict(extra="forbid")

    tool: str
    collected_at: str | None = None
    step_id: str | None = None


class PlatformOverview(BaseModel):
    """单平台当期概览指标；缺失字段保留 null，不以 0 填补。"""

    model_config = ConfigDict(extra="forbid")

    platform: str
    mentions: float | None = None
    exposure: float | None = None
    interactions: float | None = None


class MetricComparison(BaseModel):
    """单指标的当期/环比/同比数值与变化百分比（百分比由组装器按数值计算）。"""

    model_config = ConfigDict(extra="forbid")

    current: float | None = None
    mom: PeriodValue = Field(default_factory=PeriodValue)
    yoy: PeriodValue = Field(default_factory=PeriodValue)
    mom_change_pct: float | None = None
    yoy_change_pct: float | None = None


class SentimentSplit(BaseModel):
    """当期正面/中性/负面声量构成（汇总各平台）。"""

    model_config = ConfigDict(extra="forbid")

    positive: float | None = None
    neutral: float | None = None
    negative: float | None = None


class OverviewSection(BaseModel):
    """综合概览章节数据。"""

    model_config = ConfigDict(extra="forbid")

    platforms: list[PlatformOverview] = Field(default_factory=list)
    total_mentions: MetricComparison = Field(default_factory=MetricComparison)
    total_exposure: MetricComparison = Field(default_factory=MetricComparison)
    total_interactions: MetricComparison = Field(default_factory=MetricComparison)
    sentiment_split: SentimentSplit = Field(default_factory=SentimentSplit)


class SentimentRow(BaseModel):
    """平台 × 情感的声量/互动/占比（占比由组装器按平台内合计计算）。"""

    model_config = ConfigDict(extra="forbid")

    platform: str
    sentiment: str
    mentions: float | None = None
    interactions: float | None = None
    share_pct: float | None = None


class SentimentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[SentimentRow] = Field(default_factory=list)


class TrendPoint(BaseModel):
    """单日声量/互动（跨平台按日期聚合）。"""

    model_config = ConfigDict(extra="forbid")

    date: str
    mentions: float | None = None
    interactions: float | None = None


class DailyTrendSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[TrendPoint] = Field(default_factory=list)
    peak_date: str | None = None
    peak_mentions: float | None = None


class ContentTypeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: str
    mentions: float | None = None
    share_pct: float | None = None


class CreatorTierRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str
    mentions: float | None = None
    share_pct: float | None = None


class OrganicVsPaid(BaseModel):
    """自然内容 vs 商单内容声量构成。"""

    model_config = ConfigDict(extra="forbid")

    organic_mentions: float | None = None
    paid_mentions: float | None = None
    organic_share_pct: float | None = None
    paid_share_pct: float | None = None


class RegionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str
    mentions: float | None = None
    interactions: float | None = None
    share_pct: float | None = None


class TopPostRow(BaseModel):
    """单条热门原帖；除 platform 外字段均可为 null（前端/Excel 显示「未提供」）。"""

    model_config = ConfigDict(extra="forbid")

    platform: str
    post_id: str | None = None  # 平台原始帖子标识
    collected_at: str | None = None
    title: str | None = None  # 缺失保留 null，禁止填占位文本
    author: str | None = None
    interactions: int | None = None
    exposure_count: int | None = None  # 小红书=阅读数 / 抖音=播放数
    like_count: int | None = None
    comment_count: int | None = None
    collect_count: int | None = None  # 仅源字段存在时展示
    share_count: int | None = None  # 小红书=转发 / 抖音=分享
    sentiment: str | None = None
    creator_tier: str | None = None
    url: str | None = None  # 仅 MCP 返回的合法 URL，禁止拼接猜测


class BrandReportData(BaseModel):
    """归一化后的全部章节数据（唯一数值事实来源）。"""

    model_config = ConfigDict(extra="forbid")

    overview: OverviewSection = Field(default_factory=OverviewSection)
    sentiment: SentimentSection = Field(default_factory=SentimentSection)
    daily_trend: DailyTrendSection = Field(default_factory=DailyTrendSection)
    content_types: list[ContentTypeRow] = Field(default_factory=list)
    creator_tiers: list[CreatorTierRow] = Field(default_factory=list)
    organic_vs_paid: OrganicVsPaid = Field(default_factory=OrganicVsPaid)
    regions: list[RegionRow] = Field(default_factory=list)  # ≤20，声量降序
    top_posts: list[TopPostRow] = Field(default_factory=list)  # 每平台 ≤15，互动量降序


# 叙事列表单项长度上限（与 compat 文档 MarkdownBlock.text ≤20000 对齐留余量）。
_NarrativeItem = Annotated[str, Field(max_length=500)]


class BrandReportNarrative(BaseModel):
    """叙事层输出（Task 5，brand_narrative.build_brand_narrative 由模型撰写）。

    定义在本模块而非 brand_narrative.py：后者需要 import BrandReportPayload，
    模型放这里可避免双向 import 循环。字段上限与下游兼容文档约束对齐
    （ReportDocument.conclusion ≤4000、MarkdownBlock.text ≤20000、列表条数
    受块构造上限约束），防止合法叙事打爆 compat 文档。
    """

    model_config = ConfigDict(extra="forbid")

    praise_points: list[_NarrativeItem] = Field(default_factory=list, max_length=10)  # 好评点
    complaint_points: list[_NarrativeItem] = Field(default_factory=list, max_length=10)  # 槽点
    impact_level: Literal["低", "中", "高"] = "低"  # 负面影响程度
    expansion_signals: list[_NarrativeItem] = Field(default_factory=list, max_length=10)  # 扩张信号
    noise_notes: str | None = Field(default=None, max_length=2000)  # 噪音说明
    key_findings: list[_NarrativeItem] = Field(default_factory=list, max_length=10)  # 情感关键发现
    conclusion: str = Field(min_length=1, max_length=4000)  # AI 结论（必填非空）
    recommendations: list[_NarrativeItem] = Field(
        default_factory=list, max_length=10
    )  # 结论与建议


class BrandReportPayload(BaseModel):
    """brand_report_v2 顶层快照。"""

    model_config = ConfigDict(extra="forbid")

    template_version: Literal["brand_report_v2"] = "brand_report_v2"
    data_status: Literal["complete", "partial"]  # 7 个数据章节全 complete 才为 complete
    scope: ReportScope
    query_spec: QuerySpec
    data: BrandReportData
    # Task 5 叙事层回填（build_brand_narrative 撰写，数值只能引用 data）。
    narrative: BrandReportNarrative | None = None
    availability: dict[str, ChapterAvailability]  # 8 章节键，见 ALL_CHAPTERS
    sources: list[SourceEntry] = Field(default_factory=list)


__all__ = [
    "ALL_CHAPTERS",
    "DATA_CHAPTERS",
    "BrandReportData",
    "BrandReportNarrative",
    "BrandReportPayload",
    "ChapterAvailability",
    "ContentTypeRow",
    "CreatorTierRow",
    "DailyTrendSection",
    "MetricComparison",
    "OrganicVsPaid",
    "OverviewSection",
    "PeriodValue",
    "PlatformOverview",
    "QuerySpec",
    "RegionRow",
    "ReportScope",
    "SentimentRow",
    "SentimentSection",
    "SentimentSplit",
    "SourceEntry",
    "TopPostRow",
    "TrendPoint",
]
