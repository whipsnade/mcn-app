from __future__ import annotations

from collections import Counter
import json
from typing import Any, Iterable


def _response(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
            return payload["current_message"]
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
    latest_by_task: dict[str, tuple[int, Any]] = {}
    for row in rows:
        key = str(row.task_id or row.id)
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
