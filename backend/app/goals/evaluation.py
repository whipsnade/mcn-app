from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, Iterable

from app.core.redaction import redact_for_log
from app.goals.logs import (
    evaluate_goal_planner_log,
    goal_planner_request_context,
    group_goal_planner_logs,
)


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = redact_for_log(value)
    return rendered if isinstance(rendered, str) else None


def _project_period(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in ("start", "end"):
        rendered = _safe_text(value.get(key))
        if rendered is not None:
            result[key] = rendered
    return result


def _project_params(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in ("brand", "campaign", "requirement"):
        if value.get(key) is None and key in value:
            result[key] = None
            continue
        rendered = _safe_text(value.get(key))
        if rendered is not None:
            result[key] = rendered
    if "period" in value:
        result["period"] = _project_period(value.get("period"))
    platforms = value.get("platforms")
    if isinstance(platforms, list):
        result["platforms"] = [
            rendered
            for item in platforms
            if (rendered := _safe_text(item)) is not None
        ]
    return result


def _project_goal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    sequence = value.get("sequence")
    if type(sequence) is int:
        result["sequence"] = sequence
    for key in ("goal_type", "request_evidence"):
        rendered = _safe_text(value.get(key))
        if rendered is not None:
            result[key] = rendered
    dependency = value.get("depends_on_sequence")
    if dependency is None and "depends_on_sequence" in value:
        result["depends_on_sequence"] = None
    elif type(dependency) is int:
        result["depends_on_sequence"] = dependency
    if "params" in value:
        result["params"] = _project_params(value.get("params"))
    return result


def _project_question(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    text = _safe_text(value.get("text"))
    if text is not None:
        result["text"] = text
    options = value.get("options")
    if isinstance(options, list):
        result["options"] = [
            rendered
            for item in options
            if (rendered := _safe_text(item)) is not None
        ]
    return result


def _response(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}

    result: dict[str, Any] = {}
    for key in ("action", "active_brand", "brand_source"):
        if decoded.get(key) is None and key in decoded:
            result[key] = None
            continue
        rendered = _safe_text(decoded.get(key))
        if rendered is not None:
            result[key] = rendered
    if "question" in decoded:
        result["question"] = _project_question(decoded.get("question"))
    goals = decoded.get("goals")
    if isinstance(goals, list):
        result["goals"] = [
            rendered
            for goal in goals
            if (rendered := _project_goal(goal)) is not None
        ]
    return result


def _current_message(value: str | None) -> str:
    current_message = goal_planner_request_context(value).current_message
    redacted = redact_for_log(current_message)
    return redacted if isinstance(redacted, str) else ""


def summarize_goal_planner_logs(
    rows: Iterable[Any],
    *,
    limit: int | None = None,
) -> dict[str, object]:
    statuses: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    goal_types: Counter[str] = Counter()
    brand_sources: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    total_duration_ms = 0
    duration_count = 0

    for group in group_goal_planner_logs(rows, limit=limit):
        row = group.final_row
        outcome = evaluate_goal_planner_log(row)
        statuses[outcome.status] += 1
        payload = _response(row.response)
        if outcome.status == "success":
            action = payload.get("action")
            if isinstance(action, str):
                actions[action] += 1
            brand_source = payload.get("brand_source")
            if isinstance(brand_source, str):
                brand_sources[brand_source] += 1
            goals = payload.get("goals")
            if isinstance(goals, list):
                for goal in goals:
                    if isinstance(goal, dict) and isinstance(
                        goal.get("goal_type"), str
                    ):
                        goal_types[goal["goal_type"]] += 1
        if group.total_duration_ms is not None:
            total_duration_ms += group.total_duration_ms
            duration_count += 1
        samples.append(
            {
                "log_id": row.id,
                "task_id": row.task_id,
                "status": outcome.status,
                "error_code": outcome.error_code,
                "current_message": _current_message(row.messages),
                "response": payload,
            }
        )

    return {
        "total": len(samples),
        "statuses": dict(statuses),
        "actions": dict(actions),
        "goal_types": dict(goal_types),
        "brand_sources": dict(brand_sources),
        "average_duration_ms": (
            round(total_duration_ms / duration_count) if duration_count else None
        ),
        "samples": samples,
    }
