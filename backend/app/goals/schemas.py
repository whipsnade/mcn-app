from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GoalType = Literal["brand_analysis", "campaign_analysis", "kol_selection"]
BrandSource = Literal["explicit", "session", "account", "none"]


class GoalPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)


class GoalParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: str | None = Field(default=None, min_length=1, max_length=100)
    campaign: str | None = Field(default=None, min_length=1, max_length=120)
    period: GoalPeriod | None = None
    platforms: list[str] = Field(default_factory=list, max_length=5)
    requirement: str = Field(default="", max_length=1000)


class GoalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1, le=3)
    goal_type: GoalType
    depends_on_sequence: int | None = Field(default=None, ge=1, le=3)
    params: GoalParams
    request_evidence: str = Field(min_length=1, max_length=500)


class GoalQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=4)


class GoalPlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["clarify", "execute"]
    question: GoalQuestion | None = None
    goals: list[GoalSpec] = Field(default_factory=list, max_length=3)
    active_brand: str | None = Field(default=None, min_length=1, max_length=100)
    brand_source: BrandSource = "none"

    @model_validator(mode="after")
    def validate_action_shape(self) -> "GoalPlannerOutput":
        if self.action == "clarify":
            if self.question is None or self.goals:
                raise ValueError("clarify_shape_invalid")
            return self
        if self.question is not None or not self.goals:
            raise ValueError("execute_shape_invalid")
        return self
