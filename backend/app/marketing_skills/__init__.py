"""Database-backed marketing Skill revisions and activation policies."""

from app.marketing_skills.models import SkillActivation, SkillRevision
from app.marketing_skills.repository import ResolvedSkillRevision, resolve_active_revisions
from app.marketing_skills.validation import (
    SkillValidationError,
    SkillValidationResult,
    canonical_skill_digest,
    validate_skill_content,
)

__all__ = [
    "ResolvedSkillRevision",
    "SkillActivation",
    "SkillRevision",
    "SkillValidationError",
    "SkillValidationResult",
    "canonical_skill_digest",
    "resolve_active_revisions",
    "validate_skill_content",
]
