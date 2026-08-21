"""`analysis_report_v1` 的通用营销报告强类型契约。

该契约只描述可安全展示的业务值与布局，不携带 Evidence、租户、Run 或
Artifact Version 身份。业务行数不设 Top20/Top40 门禁；文件体积、分页和
工作簿 cell 限制由后续 exporter 的技术配置负责。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from app.agent_artifacts.payloads.common import HttpUrl, Limitation, Methodology, SectionAvailability

ReportColumnType = Literal[
    "string",
    "integer",
    "number",
    "percent",
    "date",
    "datetime",
    "url",
    "boolean",
]
ReportCell = StrictStr | StrictInt | StrictFloat | StrictBool | date | datetime | None

_BLOCK_ID = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_SECRET_PATTERNS = (
    re.compile(r"\bsk(?:-proj)?-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:mysql(?:\+[A-Za-z0-9_-]+)?|postgres(?:ql)?|mongodb|redis)://[^\s]+", re.IGNORECASE),
)
_ABSOLUTE_PATH = re.compile(r"(?:^|\s)/(?:tmp|private/tmp|etc|var)/")


def _reject_unsafe_text(value: Any, path: str = "$") -> None:
    if isinstance(value, BaseModel):
        _reject_unsafe_text(value.model_dump(mode="json"), path)
        return
    if isinstance(value, str):
        if value.lstrip().startswith("="):
            raise ValueError(f"formula is not allowed at {path}")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"sensitive content is not allowed at {path}")
        if _ABSOLUTE_PATH.search(value):
            raise ValueError(f"absolute path is not allowed at {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_unsafe_text(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_unsafe_text(item, f"{path}.{index}")


class AnalysisReportMetricCard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=_SAFE_KEY.pattern)
    label: str = Field(min_length=1, max_length=256)
    value: ReportCell
    unit: str | None = None
    value_type: ReportColumnType | None = None


class AnalysisReportMetricCardsBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["metric_cards"]
    id: str = Field(pattern=_BLOCK_ID.pattern)
    title: str = Field(min_length=1, max_length=256)
    cards: tuple[AnalysisReportMetricCard, ...] = ()


class AnalysisReportTableColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=_SAFE_KEY.pattern)
    label: str = Field(min_length=1, max_length=256)
    type: ReportColumnType
    width: float | None = Field(default=None, gt=0, le=200)


class AnalysisReportTypedTableBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["typed_table"]
    id: str = Field(pattern=_BLOCK_ID.pattern)
    title: str = Field(min_length=1, max_length=256)
    columns: tuple[AnalysisReportTableColumn, ...]
    rows: tuple[tuple[ReportCell, ...], ...] = ()

    @model_validator(mode="after")
    def validate_rows_and_columns(self) -> AnalysisReportTypedTableBlock:
        column_keys = [column.key for column in self.columns]
        if len(set(column_keys)) != len(column_keys):
            raise ValueError("typed_table column keys must be unique")
        for row_index, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise ValueError(f"typed_table row {row_index} length does not match columns")
            for column, cell in zip(self.columns, row, strict=True):
                _validate_table_cell(column.type, cell, row_index)
        return self


class AnalysisReportTimeSeriesPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: str | date | datetime
    values: dict[str, StrictInt | StrictFloat | None]


class AnalysisReportTimeSeriesBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["time_series"]
    id: str = Field(pattern=_BLOCK_ID.pattern)
    title: str = Field(min_length=1, max_length=256)
    points: tuple[AnalysisReportTimeSeriesPoint, ...] = ()


class AnalysisReportLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=256)
    url: HttpUrl
    description: str | None = None


class AnalysisReportLinkListBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["link_list"]
    id: str = Field(pattern=_BLOCK_ID.pattern)
    title: str = Field(min_length=1, max_length=256)
    items: tuple[AnalysisReportLink, ...] = ()


class AnalysisReportChartSeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=_SAFE_KEY.pattern)
    label: str = Field(min_length=1, max_length=256)
    values: tuple[StrictInt | StrictFloat | None, ...] = ()


class AnalysisReportChartBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["chart"]
    id: str = Field(pattern=_BLOCK_ID.pattern)
    title: str = Field(min_length=1, max_length=256)
    chart_type: Literal["bar", "line", "area", "pie"]
    categories: tuple[str, ...] = ()
    series: tuple[AnalysisReportChartSeries, ...] = ()

    @model_validator(mode="after")
    def validate_series(self) -> AnalysisReportChartBlock:
        keys = [item.key for item in self.series]
        if len(set(keys)) != len(keys):
            raise ValueError("chart series keys must be unique")
        if any(len(item.values) != len(self.categories) for item in self.series):
            raise ValueError("chart series values must match categories")
        return self


class AnalysisReportNarrativeBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["narrative"]
    id: str = Field(pattern=_BLOCK_ID.pattern)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1)
    supporting_paths: tuple[str, ...] = ()


class AnalysisReportMethodologyLimitationsBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_type: Literal["methodology_limitations"]
    id: str = Field(pattern=_BLOCK_ID.pattern)
    title: str = Field(min_length=1, max_length=256)
    methodology: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()


AnalysisReportBlock = Annotated[
    Union[
        AnalysisReportMetricCardsBlock,
        AnalysisReportTypedTableBlock,
        AnalysisReportTimeSeriesBlock,
        AnalysisReportLinkListBlock,
        AnalysisReportChartBlock,
        AnalysisReportNarrativeBlock,
        AnalysisReportMethodologyLimitationsBlock,
    ],
    Field(discriminator="block_type"),
]


class AnalysisReportFulfillment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=_SAFE_KEY.pattern)
    requested_min: int = Field(ge=0)
    actual_count: int = Field(ge=0)
    status: Literal["complete", "partial", "unavailable"]
    reason: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_count_status(self) -> AnalysisReportFulfillment:
        if self.status == "complete" and self.actual_count < self.requested_min:
            raise ValueError("complete fulfillment cannot under-deliver requested_min")
        if self.status == "partial" and self.actual_count >= self.requested_min:
            raise ValueError("partial fulfillment must under-deliver requested_min")
        if self.status == "unavailable" and self.actual_count != 0:
            raise ValueError("unavailable fulfillment must have actual_count=0")
        return self


class AnalysisReportWorkbookColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=_SAFE_KEY.pattern)
    label: str = Field(min_length=1, max_length=256)
    width: float | None = Field(default=None, gt=0, le=200)
    number_format: str | None = None


class AnalysisReportWorkbookSheet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=_SAFE_KEY.pattern)
    title: str = Field(min_length=1, max_length=31)
    block_ids: tuple[str, ...] = ()
    columns: tuple[AnalysisReportWorkbookColumn, ...] = ()
    freeze_rows: int = Field(default=1, ge=0)
    auto_filter: bool = True
    sort_by: tuple[str, ...] = ()
    page_size: int | None = Field(default=None, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_sort_by(self) -> AnalysisReportWorkbookSheet:
        if len(set(self.sort_by)) != len(self.sort_by):
            raise ValueError("workbook sort_by keys must be unique")
        if any(not _SAFE_KEY.fullmatch(key) for key in self.sort_by):
            raise ValueError("workbook sort_by key is invalid")
        if self.columns and not set(self.sort_by) <= {column.key for column in self.columns}:
            raise ValueError("workbook sort_by must reference selected columns")
        return self


class AnalysisReportWorkbookLayout(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["workbook_v1"] = "workbook_v1"
    sheets: tuple[AnalysisReportWorkbookSheet, ...] = ()


class AnalysisReportV1(BaseModel):
    """已组装的通用 Report Version payload；身份由 ArtifactService 另行保存。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["analysis_report_v1"] = "analysis_report_v1"
    module: Literal["report"] = "report"
    data_status: Literal["complete", "restricted"]
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...] = ()
    methodology: Methodology
    title: str = Field(min_length=1, max_length=512)
    subject_type: Literal["brand", "campaign", "kol", "mixed"]
    scope: dict[str, Any]
    blocks: tuple[AnalysisReportBlock, ...] = ()
    fulfillment: tuple[AnalysisReportFulfillment, ...] = ()
    workbook: AnalysisReportWorkbookLayout | None = None

    @model_validator(mode="after")
    def validate_report(self) -> AnalysisReportV1:
        required = ("blocks", "fulfillment")
        missing = [section for section in required if section not in self.availability]
        if missing:
            raise ValueError(f"availability is missing required sections: {missing}")
        expected_status = (
            "complete"
            if all(self.availability[section].status == "complete" for section in required)
            else "restricted"
        )
        if self.data_status != expected_status:
            raise ValueError("data_status must be derived from availability")
        block_ids = [block.id for block in self.blocks]
        if len(set(block_ids)) != len(block_ids):
            raise ValueError("analysis report block ids must be unique")
        fulfillment_status = _aggregate_fulfillment_status(self.fulfillment)
        if self.availability["fulfillment"].status != fulfillment_status:
            raise ValueError("fulfillment availability must match fulfillment statuses")
        for section, availability in self.availability.items():
            if availability.status != "complete" and not _has_limitation(self.limitations, section):
                raise ValueError(f"restricted section {section!r} requires a limitation")
        if self.workbook is not None:
            known_ids = set(block_ids)
            for sheet in self.workbook.sheets:
                if not set(sheet.block_ids) <= known_ids:
                    raise ValueError("workbook references an unknown report block")
                column_keys = [column.key for column in sheet.columns]
                if len(set(column_keys)) != len(column_keys):
                    raise ValueError("workbook column keys must be unique")
            _validate_workbook_sort_by(self.blocks, self.workbook)
        self.validate_limits()
        _reject_unsafe_text(self.scope, "scope")
        _reject_unsafe_text(self.blocks, "blocks")
        _reject_unsafe_text(self.fulfillment, "fulfillment")
        _reject_unsafe_text(self.title, "title")
        _reject_unsafe_text(self.limitations, "limitations")
        _reject_unsafe_text(self.methodology, "methodology")
        _reject_unsafe_text(self.workbook, "workbook")
        return self

    def validate_limits(
        self,
        *,
        max_blocks: int = 128,
        max_columns: int = 256,
        max_rows: int = 100_000,
        max_cell_chars: int = 32_767,
    ) -> AnalysisReportV1:
        _validate_technical_limits(
            self.blocks,
            self.workbook,
            max_blocks=max_blocks,
            max_columns=max_columns,
            max_rows=max_rows,
            max_cell_chars=max_cell_chars,
        )
        return self


def _validate_table_cell(column_type: str, cell: Any, row_index: int) -> None:
    if cell is None:
        return
    if column_type == "string" and not isinstance(cell, str):
        raise ValueError(f"typed_table row {row_index} string cell must be string")
    if column_type == "integer" and (not isinstance(cell, int) or isinstance(cell, bool)):
        raise ValueError(f"typed_table row {row_index} integer cell must be integer")
    if column_type in {"number", "percent"} and (
        not isinstance(cell, (int, float)) or isinstance(cell, bool)
    ):
        raise ValueError(f"typed_table row {row_index} numeric cell must be number")
    if column_type == "boolean" and not isinstance(cell, bool):
        raise ValueError(f"typed_table row {row_index} boolean cell must be boolean")
    if column_type == "date":
        if not isinstance(cell, (str, date)) or isinstance(cell, datetime):
            raise ValueError(f"typed_table row {row_index} date cell is invalid")
        try:
            date.fromisoformat(cell if isinstance(cell, str) else cell.isoformat())
        except ValueError as exc:
            raise ValueError(f"typed_table row {row_index} date cell is invalid") from exc
    if column_type == "datetime":
        if not isinstance(cell, (str, datetime)):
            raise ValueError(f"typed_table row {row_index} datetime cell is invalid")
        try:
            datetime.fromisoformat(cell if isinstance(cell, str) else cell.isoformat())
        except ValueError as exc:
            raise ValueError(f"typed_table row {row_index} datetime cell is invalid") from exc
    if column_type == "url":
        if not isinstance(cell, str) or urlsplit(cell).scheme not in {"http", "https"}:
            raise ValueError(f"typed_table row {row_index} url cell must use http/https")


def _aggregate_fulfillment_status(
    fulfillment: tuple[AnalysisReportFulfillment, ...],
) -> Literal["complete", "partial", "unavailable"]:
    if any(item.status == "unavailable" for item in fulfillment):
        return "unavailable"
    if any(item.status == "partial" for item in fulfillment):
        return "partial"
    return "complete"


def _has_limitation(limitations: tuple[Limitation, ...], section: str) -> bool:
    return any(
        not item.affected_paths
        or section in item.affected_paths
        or f"data.{section}" in item.affected_paths
        or f"/data/{section}" in item.affected_paths
        for item in limitations
    )


def _validate_workbook_sort_by(
    blocks: tuple[AnalysisReportBlock, ...],
    workbook: AnalysisReportWorkbookLayout,
) -> None:
    blocks_by_id = {block.id: block for block in blocks}
    for sheet in workbook.sheets:
        if not sheet.sort_by:
            continue
        typed_tables = [
            blocks_by_id[block_id]
            for block_id in sheet.block_ids
            if isinstance(blocks_by_id.get(block_id), AnalysisReportTypedTableBlock)
        ]
        if not typed_tables:
            raise ValueError("workbook sort_by requires a selected typed table")
        selected_keys = {column.key for column in sheet.columns}
        for table in typed_tables:
            table_keys = {column.key for column in table.columns}
            if selected_keys and not selected_keys <= table_keys:
                raise ValueError("workbook columns must reference every selected table")
            if not set(sheet.sort_by) <= table_keys:
                raise ValueError("workbook sort_by must reference every selected table")


def _validate_technical_limits(
    blocks: tuple[AnalysisReportBlock, ...],
    workbook: AnalysisReportWorkbookLayout | None,
    *,
    max_blocks: int,
    max_columns: int,
    max_rows: int,
    max_cell_chars: int,
) -> None:
    if len(blocks) > max_blocks:
        raise ValueError("analysis report block limit exceeded")
    column_count = 0
    row_count = 0
    for block in blocks:
        if isinstance(block, AnalysisReportTypedTableBlock):
            column_count += len(block.columns)
            row_count += len(block.rows)
        elif isinstance(block, AnalysisReportChartBlock):
            column_count += 1 + len(block.series)
            row_count += len(block.categories)
        elif isinstance(block, AnalysisReportMetricCardsBlock):
            column_count += 3
            row_count += len(block.cards)
        elif isinstance(block, AnalysisReportTimeSeriesBlock):
            column_count += 1 + max((len(point.values) for point in block.points), default=0)
            row_count += len(block.points)
        elif isinstance(block, AnalysisReportLinkListBlock):
            column_count += 3
            row_count += len(block.items)
        elif isinstance(block, AnalysisReportNarrativeBlock):
            column_count += 2
            row_count += 1
        elif isinstance(block, AnalysisReportMethodologyLimitationsBlock):
            column_count += 2
            row_count += 1 + len(block.limitations)
        if _max_text_length(block) > max_cell_chars:
            raise ValueError("analysis report cell text limit exceeded")
    if workbook is not None:
        column_count += sum(len(sheet.columns) for sheet in workbook.sheets)
    if column_count > max_columns:
        raise ValueError("analysis report column limit exceeded")
    if row_count > max_rows:
        raise ValueError("analysis report row limit exceeded")


def _max_text_length(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, BaseModel):
        return _max_text_length(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return max((_max_text_length(item) for item in value.values()), default=0)
    if isinstance(value, (list, tuple)):
        return max((_max_text_length(item) for item in value), default=0)
    return 0


__all__ = [
    "AnalysisReportBlock",
    "AnalysisReportChartBlock",
    "AnalysisReportFulfillment",
    "AnalysisReportLinkListBlock",
    "AnalysisReportMetricCardsBlock",
    "AnalysisReportMethodologyLimitationsBlock",
    "AnalysisReportNarrativeBlock",
    "AnalysisReportTimeSeriesBlock",
    "AnalysisReportTypedTableBlock",
    "AnalysisReportV1",
    "AnalysisReportWorkbookLayout",
    "AnalysisReportWorkbookSheet",
    "ReportCell",
    "ReportColumnType",
    "WorkbookColumn",
    "WorkbookLayout",
    "WorkbookSheet",
    "_aggregate_fulfillment_status",
    "_has_limitation",
    "_reject_unsafe_text",
    "_validate_technical_limits",
    "_validate_workbook_sort_by",
]


# Exporter-facing short aliases keep the layout contract readable without
# duplicating the payload models or introducing a second source of truth.
WorkbookColumn = AnalysisReportWorkbookColumn
WorkbookLayout = AnalysisReportWorkbookLayout
WorkbookSheet = AnalysisReportWorkbookSheet
