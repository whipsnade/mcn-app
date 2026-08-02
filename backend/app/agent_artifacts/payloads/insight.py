"""insight_board_v1: 通用钻取强类型 payload (spec §12.2).

The board requires module/title/scope/parent_artifact_id and holds a sequence of
blocks limited to eight types. Blocks carry only typed content; numeric cells are
intended to reference Evidence (lineage enforced by later tasks).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_artifacts.payloads.common import (
    ArtifactPayloadBase,
    HttpUrl,
    Period,
    validate_unique,
)

ScalarCell = int | float | str


class InsightScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = ""
    period: Period | None = None
    platforms: tuple[str, ...] = Field(default_factory=tuple)
    brand: str | None = None
    campaign: str | None = None
    kol_uid: str | None = None


# ------------------------------------------------------------------ blocks


class MetricCard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    value: ScalarCell
    unit: str | None = None
    path: str | None = None


class MetricGridBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["metric_grid"]
    title: str
    cards: tuple[MetricCard, ...] = Field(default_factory=tuple, max_length=16)


class TableBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["table"]
    title: str
    columns: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    rows: tuple[tuple[ScalarCell, ...], ...] = Field(default_factory=tuple, max_length=200)

    @model_validator(mode="after")
    def _validate_unique_columns(self) -> TableBlock:
        validate_unique(self.columns, (), "table.columns")
        return self


class ChartSeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    values: tuple[float, ...] = Field(default_factory=tuple, max_length=200)


class BarChartBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["bar_chart"]
    title: str
    categories: tuple[str, ...] = Field(default_factory=tuple)
    series: tuple[ChartSeries, ...] = Field(default_factory=tuple, max_length=20)


class LineChartBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["line_chart"]
    title: str
    x_labels: tuple[str, ...] = Field(default_factory=tuple)
    series: tuple[ChartSeries, ...] = Field(default_factory=tuple, max_length=20)


class PieSlice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float


class PieChartBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["pie_chart"]
    title: str
    slices: tuple[PieSlice, ...] = Field(default_factory=tuple, max_length=20)


class MarkdownBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["markdown"]
    title: str
    content: str


class TimelineItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    title: str
    description: str = ""


class TimelineBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["timeline"]
    title: str
    items: tuple[TimelineItem, ...] = Field(default_factory=tuple, max_length=100)


class ReferenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    url: HttpUrl


class ReferencesBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["references"]
    title: str
    items: tuple[ReferenceItem, ...] = Field(default_factory=tuple, max_length=100)


InsightBlock = Annotated[
    Union[
        MetricGridBlock,
        TableBlock,
        BarChartBlock,
        LineChartBlock,
        PieChartBlock,
        MarkdownBlock,
        TimelineBlock,
        ReferencesBlock,
    ],
    Field(discriminator="block_type"),
]


# ------------------------------------------------------------------ board


class InsightFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    detail: str
    supporting_paths: tuple[str, ...] = Field(default_factory=tuple)


class InsightNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    findings: tuple[InsightFinding, ...] = Field(default_factory=tuple)


class InsightBoardV1(ArtifactPayloadBase):
    schema_version: Literal["insight_board_v1"] = "insight_board_v1"

    title: str
    scope: InsightScope
    parent_artifact_id: str
    narrative: InsightNarrative
    data: tuple[InsightBlock, ...] = Field(default_factory=tuple, max_length=50)

    REQUIRED_SECTIONS = frozenset({"blocks"})
    SECTION_NUMERIC_PATHS = {}
