from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, Iterable

from app.core.redaction import redact_for_log


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
    if not value:
        return ""
    try:
        messages = json.loads(value)
    except ValueError:
        return ""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("current_message"), str):
            redacted = redact_for_log(payload["current_message"])
            return redacted if isinstance(redacted, str) else ""
    return ""


def _attempt(tags: Any) -> int:
    if not isinstance(tags, list):
        return 1
    prefix = "goal_planner:attempt:"
    for tag in tags:
        text = str(tag)
        if text.startswith(prefix) and text.removeprefix(prefix).isdigit():
            return int(text.removeprefix(prefix))
    return 1


def summarize_goal_planner_logs(rows: Iterable[Any]) -> dict[str, object]:
    statuses: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    goal_types: Counter[str] = Counter()
    brand_sources: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    total_duration_ms = 0
    duration_count = 0

    # 同一任务发生语义修复时只统计 attempt 最大的一次，避免 MySQL DATETIME
    # 秒级精度导致两条日志同秒、created_at 排序无法可靠区分先后。
    latest_by_task: dict[tuple[str, str], tuple[int, Any]] = {}
    for row in rows:
        key = (
            ("task", str(row.task_id))
            if row.task_id is not None
            else ("log", str(row.id))
        )
        attempt = _attempt(row.tags)
        existing = latest_by_task.get(key)
        if existing is None or attempt > existing[0]:
            latest_by_task[key] = (attempt, row)

    for _, row in latest_by_task.values():
        statuses[str(row.status)] += 1
        payload = _response(row.response)
        action = payload.get("action")
        if isinstance(action, str):
            actions[action] += 1
        brand_source = payload.get("brand_source")
        if isinstance(brand_source, str):
            brand_sources[brand_source] += 1
        goals = payload.get("goals")
        if isinstance(goals, list):
            for goal in goals:
                if isinstance(goal, dict) and isinstance(goal.get("goal_type"), str):
                    goal_types[goal["goal_type"]] += 1
        if isinstance(row.duration_ms, int):
            total_duration_ms += row.duration_ms
            duration_count += 1
        samples.append(
            {
                "log_id": row.id,
                "task_id": row.task_id,
                "status": row.status,
                "error_code": row.error_code,
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
