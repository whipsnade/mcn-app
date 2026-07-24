from __future__ import annotations

import re
import unicodedata

from app.goals.schemas import GoalPlannerOutput


class GoalPlanSemanticError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.strip()).casefold()
    return re.sub(r"\s+", "", normalized)


def _nonblank(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


_SELECTION_INTENT_PATTERNS = (
    re.compile(
        r"(?:圈选|筛选|推荐|寻找|找出|找|挑选|选出|物色)"
        r".{0,12}(?:达人|博主|kol|创作者|红人|账号)"
    ),
    re.compile(
        r"(?:形成|生成|输出|给出|整理|提供|建立)"
        r".{0,12}(?:达人|博主|kol|创作者|红人)"
        r".{0,4}(?:名单|清单|候选)"
    ),
    re.compile(r"(?:候选达人|候选博主|达人候选|博主候选|达人名单|博主名单|kol名单)"),
)


def _validate_brand_resolution(
    output: GoalPlannerOutput,
    current_message: str,
    *,
    session_brand: str | None,
    account_default_brand: str | None,
) -> None:
    active_brand = _nonblank(output.active_brand)
    normalized_active = _normalized_text(active_brand)
    normalized_message = _normalized_text(current_message)
    normalized_session = _normalized_text(_nonblank(session_brand))
    normalized_account = _normalized_text(_nonblank(account_default_brand))

    if output.brand_source == "explicit":
        if not normalized_active or normalized_active not in normalized_message:
            raise GoalPlanSemanticError("brand_source_context_mismatch")
    elif output.brand_source == "session":
        if (
            not normalized_session
            or normalized_active != normalized_session
            or normalized_active in normalized_message
        ):
            raise GoalPlanSemanticError("brand_source_context_mismatch")
    elif output.brand_source == "account":
        if (
            normalized_session
            or not normalized_account
            or normalized_active != normalized_account
            or normalized_active in normalized_message
        ):
            raise GoalPlanSemanticError("brand_source_context_mismatch")
    elif active_brand or normalized_session or normalized_account:
        raise GoalPlanSemanticError("brand_source_context_mismatch")

    for goal in output.goals:
        goal_brand = _nonblank(goal.params.brand)
        if goal_brand and (
            not active_brand
            or _normalized_text(goal_brand) != normalized_active
        ):
            raise GoalPlanSemanticError("goal_brand_mismatch")


def validate_goal_plan(
    output: GoalPlannerOutput,
    current_message: str,
    *,
    session_brand: str | None = None,
    account_default_brand: str | None = None,
) -> None:
    if output.action == "clarify":
        _validate_brand_resolution(
            output,
            current_message,
            session_brand=session_brand,
            account_default_brand=account_default_brand,
        )
        return
    sequences = [goal.sequence for goal in output.goals]
    if sequences != list(range(1, len(output.goals) + 1)):
        raise GoalPlanSemanticError("goal_sequence_invalid")
    goal_types = [goal.goal_type for goal in output.goals]
    if len(goal_types) != len(set(goal_types)):
        raise GoalPlanSemanticError("duplicate_goal_type")

    message_text = _normalized_text(current_message)
    for goal in output.goals:
        dependency = goal.depends_on_sequence
        if dependency is not None and dependency >= goal.sequence:
            raise GoalPlanSemanticError("dependency_must_precede_goal")
        if goal.goal_type == "brand_analysis" and not _nonblank(goal.params.brand):
            raise GoalPlanSemanticError("brand_scope_required")
        if goal.goal_type == "campaign_analysis" and (
            not _nonblank(goal.params.brand) or not _nonblank(goal.params.campaign)
        ):
            raise GoalPlanSemanticError("campaign_scope_required")
        if goal.goal_type == "kol_selection":
            evidence = _normalized_text(goal.request_evidence)
            if not evidence or evidence not in message_text:
                raise GoalPlanSemanticError("selection_evidence_not_in_message")
            if len(evidence) < 4:
                raise GoalPlanSemanticError("selection_evidence_too_short")
            if not any(pattern.search(evidence) for pattern in _SELECTION_INTENT_PATTERNS):
                raise GoalPlanSemanticError("selection_intent_not_explicit")

    _validate_brand_resolution(
        output,
        current_message,
        session_brand=session_brand,
        account_default_brand=account_default_brand,
    )
