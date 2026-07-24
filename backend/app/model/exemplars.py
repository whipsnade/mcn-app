"""成功案例回放：从 model_prompt_logs 检索同类场景的历史成功记录。

检索策略（第一阶段）：user_id + purpose 等值，普通 purpose 再要求
status="success"，并按 tags 交集（industry:/platform:/quick: 等标签任一命中），
按创建时间倒序取最近 N 条。
每条案例抽取"模型当时选了哪个工具、填了什么参数"的关键片段，剔除
key/token 特征字段后截断到 ~1500 字符，注入后续 prompt 供模型参考。
GoalPlanner 例外：只取每个 task 最终语义成功结果，并输出匿名结构投影。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.model.models import ModelPromptLog


# 单条案例注入 prompt 的长度上限（字符）。
EXEMPLAR_MAX_CHARS = 1500
# 候选扫描窗口：取最近 N 条成功记录后在内存中做 tags 交集过滤。
_SCAN_LIMIT = 50

# 与 orchestration/context.py 同一思路：剔除任何含密钥/凭证特征的字段。
_SENSITIVE_KEY_PARTS = ("key", "token", "secret", "authorization", "credential", "password")


def _prune_sensitive(value: Any) -> Any:
    """递归剔除键名含 key/token 等敏感特征的字段（含 messages 中的内容）。"""
    if isinstance(value, dict):
        return {
            key: _prune_sensitive(item)
            for key, item in value.items()
            if not any(part in str(key).casefold() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_prune_sensitive(item) for item in value]
    return value


def _parse_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _decision_fragment(response: Any) -> dict[str, Any]:
    """从响应 JSON 抽取决策关键片段（工具名/参数/动作/结果摘要）。"""
    if not isinstance(response, dict):
        return {}
    fragment: dict[str, Any] = {}
    for key in (
        "action",
        "internal_tool_name",
        "arguments",
        "result",
        "goals",
        "active_brand",
        "brand_source",
        "question",
    ):
        if key in response:
            fragment[key] = response[key]
    return fragment


def _request_fragment(messages: Any) -> dict[str, Any]:
    """从请求 messages 抽取场景线索（feature/goal/scenario，不含工具清单全文）。"""
    if not isinstance(messages, list):
        return {}
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = _parse_json(message.get("content"))
        if not isinstance(content, dict):
            continue
        return {
            key: content[key]
            for key in ("feature", "goal", "scenario", "industries")
            if key in content
        }
    return {}


def _build_excerpt(row: ModelPromptLog) -> str:
    excerpt = {
        "request": _request_fragment(_parse_json(row.messages)),
        "response": _decision_fragment(_parse_json(row.response)),
    }
    pruned = _prune_sensitive(excerpt)
    encoded = json.dumps(pruned, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > EXEMPLAR_MAX_CHARS:
        return encoded[:EXEMPLAR_MAX_CHARS] + "…(truncated)"
    return encoded


def _goal_planner_excerpt(output: Any) -> str:
    """GoalPlanner 案例只保留匿名结构，不携带任何业务实体自由文本。"""

    goals = []
    for goal in output.goals:
        params = goal.params
        goals.append(
            {
                "sequence": goal.sequence,
                "goal_type": goal.goal_type,
                "depends_on_sequence": goal.depends_on_sequence,
                "params": {
                    "has_brand": bool(params.brand and params.brand.strip()),
                    "has_campaign": bool(params.campaign and params.campaign.strip()),
                    "has_period": params.period is not None,
                    "platform_count": len(params.platforms),
                    "has_requirement": bool(params.requirement.strip()),
                },
                "has_request_evidence": bool(goal.request_evidence.strip()),
            }
        )
    excerpt = {
        "request": {"anonymous_structure_only": True},
        "response": {
            "action": output.action,
            "brand_source": output.brand_source,
            "has_active_brand": bool(output.active_brand and output.active_brand.strip()),
            "has_question": output.question is not None,
            "question_option_count": len(output.question.options) if output.question else 0,
            "goals": goals,
        },
    }
    return json.dumps(excerpt, ensure_ascii=False, separators=(",", ":"))


def _tags_intersect(row_tags: Any, wanted: frozenset[str]) -> bool:
    if not wanted:
        return True
    if not isinstance(row_tags, list):
        return False
    return any(str(tag) in wanted for tag in row_tags)


async def find_success_exemplars(
    db: AsyncSession,
    *,
    purpose: str,
    tags: Sequence[str] = (),
    user_id: str | None = None,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """检索当前用户的同类成功案例（最近优先）。"""

    user_filter = (
        ModelPromptLog.user_id.is_(None)
        if user_id is None
        else ModelPromptLog.user_id == user_id
    )
    filters = [
        ModelPromptLog.purpose == purpose,
        user_filter,
    ]
    # GoalPlanner 的适配器 success 只代表 Schema 合法；必须把所有状态取回，
    # 按 task 的最终 attempt 重跑语义校验后才能决定是否是成功案例。
    if purpose != "goal_planner":
        filters.append(ModelPromptLog.status == "success")
    rows = list(
        (
            await db.scalars(
                select(ModelPromptLog)
                .where(*filters)
                .order_by(
                    ModelPromptLog.created_at.desc(),
                    ModelPromptLog.id.desc(),
                )
                .limit(_SCAN_LIMIT)
            )
        ).all()
    )
    wanted = frozenset(str(tag) for tag in tags)
    rows = [row for row in rows if _tags_intersect(row.tags, wanted)]
    if purpose == "goal_planner":
        # 惰性导入避免 model.exemplars 与 goals.context 的模块初始化环。
        from app.goals.logs import (
            evaluate_goal_planner_log,
            group_goal_planner_logs,
        )

        exemplars: list[dict[str, Any]] = []
        for group in group_goal_planner_logs(rows):
            outcome = evaluate_goal_planner_log(group.final_row)
            if outcome.status != "success" or outcome.output is None:
                continue
            row = group.final_row
            exemplars.append(
                {
                    "purpose": row.purpose,
                    "tags": [
                        str(tag)
                        for tag in (row.tags or [])
                        if str(tag) == "goal_planner:shadow"
                        or str(tag).startswith("goal_planner:attempt:")
                    ],
                    "excerpt": _goal_planner_excerpt(outcome.output),
                }
            )
            if len(exemplars) >= limit:
                break
        return exemplars

    exemplars: list[dict[str, Any]] = []
    for row in rows:
        exemplars.append(
            {
                "purpose": row.purpose,
                "tags": [str(tag) for tag in (row.tags or [])],
                "excerpt": _build_excerpt(row),
            }
        )
        if len(exemplars) >= limit:
            break
    return exemplars


__all__ = ["EXEMPLAR_MAX_CHARS", "find_success_exemplars"]
