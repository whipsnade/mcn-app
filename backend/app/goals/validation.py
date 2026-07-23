from __future__ import annotations

import re
import unicodedata

from app.goals.schemas import GoalPlannerOutput


class GoalPlanSemanticError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)


def validate_goal_plan(output: GoalPlannerOutput, current_message: str) -> None:
    if output.action == "clarify":
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
        if goal.goal_type == "brand_analysis" and not goal.params.brand:
            raise GoalPlanSemanticError("brand_scope_required")
        if goal.goal_type == "campaign_analysis" and (
            not goal.params.brand or not goal.params.campaign
        ):
            raise GoalPlanSemanticError("campaign_scope_required")
        if goal.goal_type == "kol_selection":
            evidence = _normalized_text(goal.request_evidence)
            if not evidence or evidence not in message_text:
                raise GoalPlanSemanticError("selection_evidence_not_in_message")
