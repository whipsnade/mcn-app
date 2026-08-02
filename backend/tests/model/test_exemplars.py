"""成功案例回放：purpose+tags 检索、失败记录排除、截断与敏感字段剔除。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.model.exemplars import EXEMPLAR_MAX_CHARS, find_success_exemplars
from app.model.models import ModelPromptLog


def _log(
    *,
    purpose: str = "quick_feature",
    status: str = "success",
    tags: list[str] | None = None,
    response: dict | str | None = None,
    messages: list[dict] | None = None,
    user_id: str | None = None,
    task_id: str | None = None,
    age_seconds: int = 0,
) -> ModelPromptLog:
    return ModelPromptLog(
        id=str(uuid4()),
        user_id=user_id,
        session_id=None,
        task_id=task_id,
        purpose=purpose,
        tags=tags or [],
        model="deepseek-v4-pro",
        messages=json.dumps(
            messages if messages is not None else [{"role": "user", "content": "{}"}],
            ensure_ascii=False,
        ),
        response=(
            json.dumps(response, ensure_ascii=False)
            if isinstance(response, dict)
            else response
        ),
        status=status,
        error_code=None,
        prompt_tokens=10,
        completion_tokens=5,
        duration_ms=100,
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=age_seconds),
    )


@pytest.mark.asyncio
async def test_find_success_exemplars_filters_by_purpose_status_and_tags(
    db_session: AsyncSession,
) -> None:
    matching = _log(
        tags=["quick:top_posts", "platform:douyin"],
        response={"action": "call_tool", "internal_tool_name": "datatap.insight.query.raw.posts.v1", "arguments": {"size": 10}},
    )
    db_session.add(matching)
    # 失败记录与其他 purpose、无 tags 交集的记录都必须排除。
    db_session.add(_log(status="failed", tags=["quick:top_posts"]))
    db_session.add(_log(purpose="brainstorm", tags=["quick:top_posts"]))
    db_session.add(_log(tags=["quick:kol_detail"]))
    await db_session.flush()

    exemplars = await find_success_exemplars(
        db_session, purpose="quick_feature", tags=["quick:top_posts"]
    )

    assert len(exemplars) == 1
    excerpt = json.loads(exemplars[0]["excerpt"])
    assert excerpt["response"]["internal_tool_name"] == "datatap.insight.query.raw.posts.v1"
    assert excerpt["response"]["arguments"] == {"size": 10}
    assert exemplars[0]["tags"] == ["quick:top_posts", "platform:douyin"]


@pytest.mark.asyncio
async def test_find_success_exemplars_returns_most_recent_with_limit(
    db_session: AsyncSession,
) -> None:
    for age in (300, 200, 100):
        db_session.add(_log(tags=["industry:美食"], age_seconds=age))
    await db_session.flush()

    exemplars = await find_success_exemplars(
        db_session, purpose="quick_feature", tags=["industry:美食"], limit=2
    )

    assert len(exemplars) == 2


@pytest.mark.asyncio
async def test_find_success_exemplars_truncates_and_prunes_sensitive_keys(
    db_session: AsyncSession,
) -> None:
    response = {
        "action": "call_tool",
        "internal_tool_name": "datatap.insight.query.raw.posts.v1",
        "arguments": {
            "name": "美食" * 4000,
            "api_key": "sk-should-never-appear",
            "access_token": "secret",
        },
    }
    db_session.add(_log(tags=["quick:top_posts"], response=response))
    await db_session.flush()

    [exemplar] = await find_success_exemplars(
        db_session, purpose="quick_feature", tags=["quick:top_posts"]
    )

    assert len(exemplar["excerpt"]) <= EXEMPLAR_MAX_CHARS + len("…(truncated)")
    assert "sk-should-never-appear" not in exemplar["excerpt"]
    assert "api_key" not in exemplar["excerpt"]
    assert "access_token" not in exemplar["excerpt"]


@pytest.mark.asyncio
async def test_find_success_exemplars_without_tags_matches_purpose_only(
    db_session: AsyncSession,
) -> None:
    db_session.add(_log(tags=["whatever"]))
    await db_session.flush()

    exemplars = await find_success_exemplars(db_session, purpose="quick_feature")

    assert len(exemplars) == 1


@pytest.mark.asyncio
async def test_find_success_exemplars_parses_think_wrapped_response(
    db_session: AsyncSession,
) -> None:
    """推理模型历史响应带 <think> 前缀时，仍能提取决策片段而不是空 excerpt。"""
    db_session.add(
        _log(
            tags=["quick:top_posts"],
            messages=[
                {
                    "role": "user",
                    "content": (
                        '<think>梳理一下场景</think>{"feature":"top_posts","goal":"找爆贴"}'
                    ),
                }
            ],
            response='<think>先推理一下</think>{"action":"finish","result":"ok"}',
        )
    )
    await db_session.flush()

    [exemplar] = await find_success_exemplars(
        db_session, purpose="quick_feature", tags=["quick:top_posts"]
    )

    excerpt = json.loads(exemplar["excerpt"])
    assert excerpt["response"]["action"] == "finish"
    assert excerpt["response"]["result"] == "ok"
    assert excerpt["request"]["feature"] == "top_posts"


@pytest.mark.asyncio
async def test_find_success_exemplars_plain_json_response_unchanged(
    db_session: AsyncSession,
) -> None:
    """回归：普通 JSON 响应的 excerpt 行为不变。"""
    db_session.add(
        _log(
            tags=["quick:top_posts"],
            response={"action": "finish", "result": "ok"},
        )
    )
    await db_session.flush()

    [exemplar] = await find_success_exemplars(
        db_session, purpose="quick_feature", tags=["quick:top_posts"]
    )

    excerpt = json.loads(exemplar["excerpt"])
    assert excerpt["response"] == {"action": "finish", "result": "ok"}
