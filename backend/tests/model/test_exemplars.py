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


def _goal_messages(
    current_message: str,
    *,
    session_brand: str | None = None,
) -> list[dict]:
    return [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "current_message": current_message,
                    "session_context": {"active_brand": session_brand},
                    "account_default_brand": None,
                },
                ensure_ascii=False,
            ),
        }
    ]


def _goal_response(
    *,
    brand: str,
    campaign: str,
    evidence: str,
) -> dict:
    return {
        "action": "execute",
        "active_brand": brand,
        "brand_source": "explicit",
        "question": None,
        "goals": [
            {
                "sequence": 1,
                "goal_type": "campaign_analysis",
                "depends_on_sequence": None,
                "params": {
                    "brand": brand,
                    "campaign": campaign,
                    "period": None,
                    "platforms": [],
                    "requirement": "",
                },
                "request_evidence": evidence,
            }
        ],
    }


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
async def test_goal_planner_exemplar_keeps_only_goal_structure(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        _log(
            purpose="goal_planner",
            tags=["goal_planner:shadow", "goal_planner:attempt:1"],
            task_id="goal-task",
            messages=_goal_messages("分析喜茶 618 表现"),
            response={
                "action": "execute",
                "active_brand": "喜茶",
                "brand_source": "explicit",
                "question": None,
                "goals": [
                    {
                        "sequence": 1,
                        "goal_type": "campaign_analysis",
                        "params": {"brand": "喜茶", "campaign": "618"},
                        "request_evidence": "分析喜茶 618 表现",
                    }
                ],
            },
        )
    )
    await db_session.flush()

    [exemplar] = await find_success_exemplars(
        db_session,
        purpose="goal_planner",
        tags=["goal_planner:shadow"],
    )

    excerpt = json.loads(exemplar["excerpt"])
    assert excerpt["response"]["has_active_brand"] is True
    assert "active_brand" not in excerpt["response"]
    assert excerpt["response"]["brand_source"] == "explicit"
    assert excerpt["response"]["goals"][0]["goal_type"] == "campaign_analysis"


@pytest.mark.asyncio
async def test_goal_planner_exemplars_are_user_scoped_and_structurally_anonymous(
    db_session: AsyncSession,
    user_factory,
) -> None:
    first_user = await user_factory()
    second_user = await user_factory()
    first_brand = "甲方私密品牌"
    second_brand = "乙方私密品牌"
    first_message = (
        "分析甲方私密品牌暑期私密活动，联系 13812345678，"
        "参考 https://private.example.invalid，token=private-token"
    )
    db_session.add_all(
        [
            _log(
                purpose="goal_planner",
                tags=["goal_planner:shadow", "goal_planner:attempt:1"],
                user_id=first_user.id,
                task_id="first-user-task",
                messages=_goal_messages(first_message),
                response=_goal_response(
                    brand=first_brand,
                    campaign="暑期私密活动",
                    evidence="分析甲方私密品牌暑期私密活动",
                ),
            ),
            _log(
                purpose="goal_planner",
                tags=["goal_planner:shadow", "goal_planner:attempt:1"],
                user_id=second_user.id,
                task_id="second-user-task",
                messages=_goal_messages("分析乙方私密品牌周年私密活动"),
                response=_goal_response(
                    brand=second_brand,
                    campaign="周年私密活动",
                    evidence="分析乙方私密品牌周年私密活动",
                ),
            ),
        ]
    )
    await db_session.flush()

    [exemplar] = await find_success_exemplars(
        db_session,
        purpose="goal_planner",
        tags=["goal_planner:shadow"],
        user_id=first_user.id,
    )

    excerpt = json.loads(exemplar["excerpt"])
    goal = excerpt["response"]["goals"][0]
    assert excerpt["response"]["action"] == "execute"
    assert excerpt["response"]["brand_source"] == "explicit"
    assert excerpt["response"]["has_active_brand"] is True
    assert goal["goal_type"] == "campaign_analysis"
    assert goal["params"] == {
        "has_brand": True,
        "has_campaign": True,
        "has_period": False,
        "platform_count": 0,
        "has_requirement": False,
    }
    assert goal["has_request_evidence"] is True
    encoded = json.dumps(excerpt, ensure_ascii=False)
    for forbidden in (
        first_brand,
        second_brand,
        "暑期私密活动",
        "周年私密活动",
        "13812345678",
        "private.example.invalid",
        "private-token",
    ):
        assert forbidden not in encoded


@pytest.mark.asyncio
async def test_goal_planner_exemplar_uses_only_final_semantic_success(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory()
    db_session.add_all(
        [
            _log(
                purpose="goal_planner",
                tags=["goal_planner:shadow", "goal_planner:attempt:1"],
                user_id=user.id,
                task_id="final-invalid-task",
                age_seconds=2,
                messages=_goal_messages("分析活动中哪些达人表现最好"),
                response={
                    "action": "clarify",
                    "active_brand": None,
                    "brand_source": "none",
                    "question": {"text": "请补充品牌", "options": []},
                    "goals": [],
                },
            ),
            _log(
                purpose="goal_planner",
                tags=["goal_planner:shadow", "goal_planner:attempt:2"],
                user_id=user.id,
                task_id="final-invalid-task",
                age_seconds=1,
                messages=_goal_messages("分析活动中哪些达人表现最好"),
                response={
                    "action": "execute",
                    "active_brand": None,
                    "brand_source": "none",
                    "question": None,
                    "goals": [
                        {
                            "sequence": 1,
                            "goal_type": "kol_selection",
                            "depends_on_sequence": None,
                            "params": {
                                "brand": None,
                                "campaign": None,
                                "period": None,
                                "platforms": [],
                                "requirement": "",
                            },
                            "request_evidence": "达人表现",
                        }
                    ],
                },
            ),
            _log(
                purpose="goal_planner",
                tags=["goal_planner:shadow", "goal_planner:attempt:1"],
                user_id=user.id,
                task_id="final-valid-task",
                messages=_goal_messages("分析公开品牌 618 活动"),
                response=_goal_response(
                    brand="公开品牌",
                    campaign="618",
                    evidence="分析公开品牌 618 活动",
                ),
            ),
        ]
    )
    await db_session.flush()

    exemplars = await find_success_exemplars(
        db_session,
        purpose="goal_planner",
        tags=["goal_planner:shadow"],
        user_id=user.id,
        limit=5,
    )

    assert len(exemplars) == 1
    assert json.loads(exemplars[0]["excerpt"])["response"]["goals"][0]["goal_type"] == (
        "campaign_analysis"
    )
