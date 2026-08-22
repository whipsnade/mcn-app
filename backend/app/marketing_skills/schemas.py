from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _SkillModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillValidationRequest(_SkillModel):
    expected_name: str | None = Field(default=None, min_length=2, max_length=96)
    content: str = Field(min_length=1, max_length=200_000)


class SkillValidationErrorRead(_SkillModel):
    code: str
    message: str
    line: int | None = None


class SkillValidationRead(_SkillModel):
    valid: bool
    name: str | None = None
    description: str | None = None
    required_tools: list[str] = Field(default_factory=list)
    artifact_contract: str | None = None
    model_input_contract_version: str = "direct_model_input_v1"
    content_digest: str
    errors: list[SkillValidationErrorRead] = Field(default_factory=list)


class SkillRevisionCreate(_SkillModel):
    content: str = Field(min_length=1, max_length=200_000)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=36)
    change_note: str | None = Field(default=None, max_length=512)


class SkillRevisionRead(_SkillModel):
    id: str
    tenant_id: str | None = None
    scope_key: str
    skill_name: str
    revision: int
    content: str
    content_digest: str
    description: str
    required_tools: list[str]
    artifact_contract: str | None = None
    model_input_contract_version: str = "direct_model_input_v1"
    created_by: str | None = None
    created_at: datetime
    change_note: str | None = None


class SkillActivationRequest(_SkillModel):
    revision: int = Field(gt=0)
    revision_id: str | None = Field(default=None, min_length=1, max_length=36)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=36)
    environment: Literal["development", "staging", "production"] = "production"
    rollout_percent: int = Field(default=100, ge=0, le=100)


class SkillRollbackRequest(_SkillModel):
    tenant_id: str | None = Field(default=None, min_length=1, max_length=36)
    environment: Literal["development", "staging", "production"] = "production"


class SkillActivationRead(_SkillModel):
    id: str
    environment: str
    tenant_id: str | None = None
    scope_key: str
    skill_name: str
    active_revision: int
    active_revision_id: str
    previous_revision: int | None = None
    previous_revision_id: str | None = None
    rollout_percent: int
    previous_rollout_percent: int | None = None
    updated_by: str | None = None
    updated_at: datetime


class SkillListItem(_SkillModel):
    skill_name: str
    latest_revision: int
    revision_count: int
    active: list[SkillActivationRead] = Field(default_factory=list)


class SkillListRead(_SkillModel):
    items: list[SkillListItem]
    total: int


class SkillDetailRead(_SkillModel):
    skill_name: str
    revisions: list[SkillRevisionRead]
    activations: list[SkillActivationRead]


class SkillDiffRead(_SkillModel):
    skill_name: str
    from_revision: int
    to_revision: int
    from_revision_id: str | None = None
    to_revision_id: str | None = None
    diff: str


__all__ = [
    "SkillActivationRead",
    "SkillActivationRequest",
    "SkillDetailRead",
    "SkillDiffRead",
    "SkillListItem",
    "SkillListRead",
    "SkillRevisionCreate",
    "SkillRevisionRead",
    "SkillRollbackRequest",
    "SkillValidationErrorRead",
    "SkillValidationRead",
    "SkillValidationRequest",
]
