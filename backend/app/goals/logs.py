from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from pydantic import ValidationError

from app.goals.schemas import GoalPlannerOutput
from app.goals.validation import GoalPlanSemanticError, validate_goal_plan


@dataclass(frozen=True)
class GoalPlannerRequestContext:
    current_message: str
    session_brand: str | None
    account_default_brand: str | None


@dataclass(frozen=True)
class GoalPlannerLogOutcome:
    status: str
    error_code: str | None
    output: GoalPlannerOutput | None


@dataclass(frozen=True)
class GoalPlannerLogGroup:
    final_row: Any
    rows: tuple[Any, ...]
    total_duration_ms: int | None


def goal_planner_attempt(tags: Any) -> int:
    if not isinstance(tags, list):
        return 1
    prefix = "goal_planner:attempt:"
    for tag in tags:
        text = str(tag)
        suffix = text.removeprefix(prefix)
        if text.startswith(prefix) and suffix.isdigit():
            return int(suffix)
    return 1


def goal_planner_log_key(row: Any) -> tuple[str, str]:
    if row.task_id is not None:
        return ("task", str(row.task_id))
    return ("log", str(row.id))


def group_goal_planner_logs(
    rows: Iterable[Any],
    *,
    limit: int | None = None,
) -> list[GoalPlannerLogGroup]:
    """按 task 聚合 attempt，并保留输入中首次出现（最新）的任务顺序。"""

    grouped: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        grouped.setdefault(goal_planner_log_key(row), []).append(row)

    result: list[GoalPlannerLogGroup] = []
    for task_rows in grouped.values():
        final_row = task_rows[0]
        final_attempt = goal_planner_attempt(final_row.tags)
        for candidate in task_rows[1:]:
            candidate_attempt = goal_planner_attempt(candidate.tags)
            if candidate_attempt > final_attempt:
                final_row = candidate
                final_attempt = candidate_attempt
        durations = [
            row.duration_ms for row in task_rows if isinstance(row.duration_ms, int)
        ]
        result.append(
            GoalPlannerLogGroup(
                final_row=final_row,
                rows=tuple(task_rows),
                total_duration_ms=sum(durations) if durations else None,
            )
        )
    return result if limit is None else result[:limit]


def _parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def goal_planner_request_context(messages_text: str | None) -> GoalPlannerRequestContext:
    messages = _parse_json(messages_text)
    if not isinstance(messages, list):
        return GoalPlannerRequestContext("", None, None)
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        payload = _parse_json(content if isinstance(content, str) else None)
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("current_message"), str
        ):
            continue
        session_context = payload.get("session_context")
        session_brand = (
            session_context.get("active_brand")
            if isinstance(session_context, Mapping)
            and isinstance(session_context.get("active_brand"), str)
            else None
        )
        account_default_brand = (
            payload.get("account_default_brand")
            if isinstance(payload.get("account_default_brand"), str)
            else None
        )
        return GoalPlannerRequestContext(
            current_message=payload["current_message"],
            session_brand=session_brand,
            account_default_brand=account_default_brand,
        )
    return GoalPlannerRequestContext("", None, None)


def evaluate_goal_planner_log(row: Any) -> GoalPlannerLogOutcome:
    """把适配器结构成功重新解释为 GoalPlanner 最终语义结果。"""

    raw_status = str(row.status)
    if raw_status != "success":
        return GoalPlannerLogOutcome(
            status=raw_status,
            error_code=row.error_code,
            output=None,
        )
    try:
        output = GoalPlannerOutput.model_validate_json(row.response, strict=True)
    except (TypeError, ValidationError):
        return GoalPlannerLogOutcome(
            status="invalid",
            error_code="goal_planner_schema_invalid",
            output=None,
        )
    request = goal_planner_request_context(row.messages)
    try:
        validate_goal_plan(
            output,
            request.current_message,
            session_brand=request.session_brand,
            account_default_brand=request.account_default_brand,
        )
    except GoalPlanSemanticError as error:
        return GoalPlannerLogOutcome(
            status="invalid",
            error_code=error.code,
            output=None,
        )
    return GoalPlannerLogOutcome(status="success", error_code=None, output=output)


__all__ = [
    "GoalPlannerLogGroup",
    "GoalPlannerLogOutcome",
    "GoalPlannerRequestContext",
    "evaluate_goal_planner_log",
    "goal_planner_attempt",
    "goal_planner_log_key",
    "goal_planner_request_context",
    "group_goal_planner_logs",
]
