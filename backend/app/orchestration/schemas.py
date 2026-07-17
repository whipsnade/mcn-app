from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.mcp_gateway.contracts import DataTapService


class PlanValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


AnalysisScope = Literal["brand", "kol", "hybrid"]
EvidenceKind = Literal["brand", "kol"]


class ToolPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^step_[0-9]+$")
    internal_tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    depends_on: tuple[str, ...] = Field(default_factory=tuple, max_length=10)
    evidence_goal: str = Field(min_length=1, max_length=300)
    # Legacy plans created before brand analysis was introduced are KOL plans.
    evidence_kind: EvidenceKind = "kol"


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=1000)
    steps: tuple[ToolPlanStep, ...] = Field(min_length=1, max_length=10)
    stop_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=10)
    # Persist the router decision with the task so reporting never has to
    # infer a brand/KOL scope from MCP output after execution.
    analysis_scope: AnalysisScope = "kol"


class ReplanFailure(BaseModel):
    """仅允许进入模型的 MCP 失败安全摘要，不包含原始响应。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(pattern=r"^step_[0-9]+$")
    internal_tool_name: str = Field(min_length=1, max_length=128)
    error_code: str = Field(min_length=1, max_length=64)
    diagnostic: dict[str, Any] | None = None


class ReplanContext(BaseModel):
    """补充计划的剩余预算和安全执行状态。"""

    model_config = ConfigDict(extra="forbid")

    completed_step_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=10)
    failed_steps: tuple[ReplanFailure, ...] = Field(min_length=1, max_length=10)
    remaining_calls: int = Field(ge=0, le=10)
    remaining_points: int = Field(ge=0)


class SessionBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    brand: str = Field(default="", max_length=100)
    campaign_name: str | None = Field(default=None, min_length=1, max_length=120)
    platforms: tuple[str, ...] = Field(default_factory=tuple, max_length=5)
    category: str = Field(min_length=1, max_length=100)
    target_audience: str = Field(default="", max_length=500)
    budget_min: Decimal | None
    budget_max: Decimal | None
    filters: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_workspace(cls, workspace: Any) -> "SessionBrief":
        return cls(
            session_id=workspace.id,
            brand=workspace.brand,
            campaign_name=workspace.campaign_name,
            platforms=tuple(workspace.platforms),
            category=workspace.category,
            target_audience=workspace.target_audience,
            budget_min=workspace.budget_min,
            budget_max=workspace.budget_max,
            filters=workspace.filters_snapshot,
        )


class PlannerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^(system|user|assistant)$")
    content: str = Field(min_length=1, max_length=24_000)
    sequence: int = Field(ge=1)


class PlannerTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_id: str = Field(min_length=1)
    internal_name: str = Field(min_length=1, max_length=128)
    service: DataTapService
    description: str = Field(default="已审核工具", min_length=1, max_length=500)
    input_schema: dict[str, Any]

    @classmethod
    def from_approved(cls, item: Any) -> "PlannerTool":
        return cls(
            catalog_id=item.catalog_id,
            internal_name=item.internal_name,
            service=item.service,
            description=getattr(item, "reviewed_description", item.internal_name),
            input_schema=item.input_schema,
        )


class ExportFieldContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=32)
    required_field_names: tuple[str, ...] = Field(min_length=1, max_length=64)
    labels: dict[str, str] = Field(default_factory=dict)
    notes: tuple[str, ...] = Field(default_factory=tuple, max_length=20)


class AnalyticsFieldContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=32)
    field_names: tuple[str, ...] = Field(min_length=1, max_length=32)
    labels: dict[str, str] = Field(default_factory=dict)
    notes: tuple[str, ...] = Field(default_factory=tuple, max_length=20)


class PlannerContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: SessionBrief
    recent_messages: tuple[PlannerMessage, ...]
    existing_results: dict[str, Any]
    tools: tuple[PlannerTool, ...]
    allowed_channels: tuple[str, ...]
    export_contract: ExportFieldContract
    analytics_contract: AnalyticsFieldContract
    analysis_scope: AnalysisScope = "kol"
    analysis_objectives: tuple[str, ...] = Field(default_factory=tuple, max_length=12)
    requested_period: dict[str, Any] = Field(default_factory=dict)
