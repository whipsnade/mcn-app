"""自由分析报告的块契约。

报告撰写模型（report_writer）只能输出这里声明的块类型；前端通用报表页
按 ``type`` 渲染，不认识业务字段。所有数字必须来自已结算的 MCP 证据。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class HeadingBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["heading"] = "heading"
    text: str = Field(min_length=1, max_length=200)


class MarkdownBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["markdown"] = "markdown"
    text: str = Field(min_length=1, max_length=20_000)


class MetricItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    value: str | int | float
    unit: str | None = Field(default=None, max_length=20)
    delta: str | None = Field(default=None, max_length=60)


class MetricGridBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["metric_grid"] = "metric_grid"
    title: str | None = Field(default=None, max_length=120)
    items: list[MetricItem] = Field(min_length=1, max_length=12)


TableCell = str | int | float | None


class TableBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["table"] = "table"
    title: str | None = Field(default=None, max_length=120)
    columns: list[str] = Field(min_length=1, max_length=12)
    rows: list[list[TableCell]] = Field(max_length=200)


class ChartSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    values: list[float | int | None] = Field(min_length=1, max_length=60)


class ChartBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bar_chart", "line_chart", "pie_chart"]
    title: str | None = Field(default=None, max_length=120)
    categories: list[str] = Field(min_length=1, max_length=60)
    series: list[ChartSeries] = Field(min_length=1, max_length=6)


class TagListBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tag_list"] = "tag_list"
    title: str | None = Field(default=None, max_length=120)
    items: list[str] = Field(min_length=1, max_length=30)


class SourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    collected_at: str | None = Field(default=None, max_length=40)
    evidence: str | None = Field(default=None, max_length=120)


class SourcesBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["sources"] = "sources"
    items: list[SourceItem] = Field(min_length=1, max_length=20)


ReportBlock = Annotated[
    HeadingBlock
    | MarkdownBlock
    | MetricGridBlock
    | TableBlock
    | ChartBlock
    | TagListBlock
    | SourcesBlock,
    Field(discriminator="type"),
]


class ReportDocument(BaseModel):
    """report_writer 的结构化输出；落库前再次校验。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    conclusion: str | None = Field(default=None, max_length=4_000)
    blocks: list[ReportBlock] = Field(min_length=1, max_length=40)


__all__ = [
    "ChartBlock",
    "ChartSeries",
    "HeadingBlock",
    "MarkdownBlock",
    "MetricGridBlock",
    "MetricItem",
    "ReportBlock",
    "ReportDocument",
    "SourceItem",
    "SourcesBlock",
    "TableBlock",
    "TagListBlock",
]
