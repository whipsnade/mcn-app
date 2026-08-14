"""``insight_board_v1`` Draft builder（H5：开放式钻取看板收口）。

UAT 取证：模型用 create_draft 手写 insight_board_v1 payload 连续失败（字段级
错误回喂也收敛不了）。与五类强类型产物同一解法——Builder 兜底：

- **模型只提交结构**：钻取问题、title/scope、板块规格（``BlockSpec`` 判别
  联合，8 种类型）与每个数字的 ``value_ref`` 引用（evidence / artifact /
  calculation 三来源），不允许直接填写数值；
- **工具层负责取值**：解析 value_ref、复制真实数值、做归属校验（见
  ``agent_runtime/tools/builders.py`` 的 ``BuildInsightDraftTool``）；
- **本 builder 负责组装**：把「已解析取值的板块 + 数字级 lineage」装配为
  payload，经 ``InsightBoardV1`` 强校验（narrative supporting_paths、scope
  extra 字段、URL scheme 等都在此拦截），输出 :class:`DraftBuildResult`。

value_ref 解析失败（路径不可解析 / 跨 Session / 计算调用未 settled）一律
fail-fast 为结构化 ``draft_build_error`` 字段级回喂——板块数值字段不可空，
无法像强类型报告那样用 null + limitation 表达部分缺失，静默丢板块又会
打断 narrative supporting_paths。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    methodology_dict,
)
from app.agent_artifacts.lineage import PointerError, resolve_pointer
from app.agent_artifacts.payloads.insight import InsightBoardV1

SCHEMA_VERSION = "insight_board_v1"

# payload 的 module 字段（ArtifactPayloadBase Literal["brand","campaign","kol"]）。
_PAYLOAD_MODULES = frozenset({"brand", "campaign", "kol"})


# ---------------------------------------------------------------------------
# value_ref：模型对单个数字的引用（不给字面值；工具层解析复制真实值）
# ---------------------------------------------------------------------------


class EvidenceValueRef(BaseModel):
    """引用当前 Session 的 Evidence 字段。"""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["evidence"] = "evidence"
    evidence_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)


class ArtifactValueRef(BaseModel):
    """引用当前 Session 已发布 Artifact Version 的字段（递归追溯到 Evidence）。"""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["artifact"] = "artifact"
    artifact_version_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)


CalculationInputRef = Annotated[
    Union[EvidenceValueRef, ArtifactValueRef],
    Field(discriminator="source_type"),
]


class CalculationValueRef(BaseModel):
    """引用当前 Session 已 settled 内部计算工具调用的结果字段。

    ``input_refs`` 是该计算的输入来源（≥1）：lineage 的 sources 基座，
    ``input_paths`` 即各 input_ref 的 source_path（与 kol_selection 评分
    derivation 同一契约）。
    """

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["calculation"] = "calculation"
    tool_call_id: str = Field(min_length=1)
    result_path: str = Field(min_length=1)
    input_refs: tuple[CalculationInputRef, ...] = Field(min_length=1)


ValueRef = Annotated[
    Union[EvidenceValueRef, ArtifactValueRef, CalculationValueRef],
    Field(discriminator="source_type"),
]


# ---------------------------------------------------------------------------
# BlockSpec：8 种板块的模型输入规格（判别字段 type；数值一律经 value_ref）
# ---------------------------------------------------------------------------


class MetricCardSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value_ref: ValueRef
    unit: str | None = None
    path: str | None = None


class MetricGridBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["metric_grid"]
    title: str
    cards: tuple[MetricCardSpec, ...] = Field(default_factory=tuple, max_length=16)


class TableCellRef(BaseModel):
    """table 数字/引用单元格：``{"value_ref": {...}}``（裸数字字面值会被拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    value_ref: ValueRef


# table 单元格：纯文本标签直接给字符串；任何数值必须经 value_ref。
TableCell = Union[str, TableCellRef]


class TableBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["table"]
    title: str
    columns: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    rows: tuple[tuple[TableCell, ...], ...] = Field(default_factory=tuple, max_length=200)


class ChartSeriesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    values: tuple[ValueRef, ...] = Field(default_factory=tuple, max_length=200)


class BarChartBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bar_chart"]
    title: str
    categories: tuple[str, ...] = Field(default_factory=tuple)
    series: tuple[ChartSeriesSpec, ...] = Field(default_factory=tuple, max_length=20)


class LineChartBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["line_chart"]
    title: str
    x_labels: tuple[str, ...] = Field(default_factory=tuple)
    series: tuple[ChartSeriesSpec, ...] = Field(default_factory=tuple, max_length=20)


class PieSliceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value_ref: ValueRef


class PieChartBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pie_chart"]
    title: str
    slices: tuple[PieSliceSpec, ...] = Field(default_factory=tuple, max_length=20)


class MarkdownBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["markdown"]
    title: str
    content: str


class TimelineItemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    title: str
    description: str = ""


class TimelineBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["timeline"]
    title: str
    items: tuple[TimelineItemSpec, ...] = Field(default_factory=tuple, max_length=100)


class ReferenceItemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    url: str


class ReferencesBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["references"]
    title: str
    items: tuple[ReferenceItemSpec, ...] = Field(default_factory=tuple, max_length=100)


BlockSpec = Annotated[
    Union[
        MetricGridBlockSpec,
        TableBlockSpec,
        BarChartBlockSpec,
        LineChartBlockSpec,
        PieChartBlockSpec,
        MarkdownBlockSpec,
        TimelineBlockSpec,
        ReferencesBlockSpec,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# 已解析板块：工具层取值后的 builder 输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLineage:
    """一个数字叶子的 lineage：payload 根相对 RFC6901 路径 + 来源 + 可选推导。"""

    artifact_path: str
    sources: list[dict[str, Any]]
    derivation: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedBlock:
    """已取值的板块：payload 形态的 block dict + 其数字叶子的 lineage。"""

    block: dict[str, Any]
    lineage: list[ResolvedLineage] = field(default_factory=list)


def build_insight_draft(
    *,
    question: str,
    title: str,
    module: str,
    scope: dict[str, Any],
    parent_artifact_id: str,
    parent_artifact_version_id: str,
    blocks: list[ResolvedBlock],
    narrative: dict[str, Any] | None = None,
    source_names: tuple[str, ...] = ("insight_evidence",),
    data_as_of: datetime | None = None,
) -> DraftBuildResult:
    """把已解析取值的板块组装为 ``insight_board_v1`` Draft。

    稳定身份走 keys.py 的 ``insight:{parent_artifact_version_id}:{question_hash}``
    规则——同一父 Version 上的同一钻取问题复用同一 Artifact，重调（Reviewer
    revise 后）追加新 Revision。

    ``narrative``：模型提供的叙事（``{summary, findings[]}``，findings 条目
    supporting_paths 必须指向 data 内真实路径），写入前经 ``InsightBoardV1``
    强校验；缺省时按钻取问题生成兜底叙事。
    """
    if module not in _PAYLOAD_MODULES:
        raise DraftBuildError(
            f"insight payload module must be one of {sorted(_PAYLOAD_MODULES)}, got {module!r}"
        )
    if not blocks:
        raise DraftBuildError("insight board requires at least one block")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "module": module,
        "data_status": "complete",
        "availability": {"blocks": {"status": "complete", "reason_codes": []}},
        "limitations": [],
        "methodology": methodology_dict(data_as_of=data_as_of, source_names=source_names),
        "title": title,
        "scope": scope,
        "parent_artifact_id": parent_artifact_id,
        "narrative": (
            narrative
            if narrative is not None
            else {"summary": f"围绕「{question}」的钻取看板。", "findings": []}
        ),
        "data": [resolved.block for resolved in blocks],
    }
    try:
        InsightBoardV1.model_validate(payload)
    except ValidationError as exc:
        raise DraftBuildError(f"invalid insight_board_v1 payload: {exc}") from exc

    # 数字级 lineage：路径必须在 payload 内可解析且唯一（builder 内部一致性
    # 自检；取值与路径构造在工具层，错位在此 fail-fast 而不是留到发布边界）。
    refs: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for resolved in blocks:
        for entry in resolved.lineage:
            if entry.artifact_path in seen_paths:
                raise DraftBuildError(
                    f"duplicate lineage artifact_path: {entry.artifact_path!r}"
                )
            seen_paths.add(entry.artifact_path)
            try:
                resolve_pointer(payload, entry.artifact_path)
            except PointerError as exc:
                raise DraftBuildError(
                    f"lineage artifact_path does not resolve in payload: {exc}"
                ) from exc
            refs.append(
                {
                    "artifact_path": entry.artifact_path,
                    "sources": entry.sources,
                    "derivation": entry.derivation,
                }
            )

    return DraftBuildResult(
        module="insight",
        schema_version=SCHEMA_VERSION,
        artifact_type=SCHEMA_VERSION,
        business_fields={
            "parent_artifact_version_id": parent_artifact_version_id,
            "question": question,
        },
        payload=payload,
        evidence_refs=refs,
        parent_artifact_id=parent_artifact_id,
        parent_artifact_version_id=parent_artifact_version_id,
    )


__all__ = [
    "SCHEMA_VERSION",
    "ArtifactValueRef",
    "BarChartBlockSpec",
    "BlockSpec",
    "CalculationValueRef",
    "ChartSeriesSpec",
    "EvidenceValueRef",
    "LineChartBlockSpec",
    "MarkdownBlockSpec",
    "MetricCardSpec",
    "MetricGridBlockSpec",
    "PieChartBlockSpec",
    "PieSliceSpec",
    "ReferenceItemSpec",
    "ReferencesBlockSpec",
    "ResolvedBlock",
    "ResolvedLineage",
    "TableBlockSpec",
    "TableCellRef",
    "TimelineBlockSpec",
    "TimelineItemSpec",
    "ValueRef",
    "build_insight_draft",
]
