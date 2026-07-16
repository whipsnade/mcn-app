import asyncio

import pytest
from pydantic import ValidationError

from app.tasks.followups import (
    FollowupSuggestion,
    FollowupSuggestions,
    contains_internal_reference,
    build_followup_request,
    safe_followup_error,
)


def _suggestion(index: int = 1, **overrides):
    value = {
        "title": f"受众地域分析建议{index}",
        "prompt": f"请进一步分析第{index}轮候选达人的受众地域分布，并按覆盖率比较结果。",
        "rationale": "用于验证下一轮受众覆盖情况。",
    }
    value.update(overrides)
    return value


def test_followup_schema_requires_exactly_five_unique_chinese_suggestions() -> None:
    result = FollowupSuggestions(suggestions=tuple(_suggestion(i) for i in range(1, 6)))
    assert len(result.suggestions) == 5
    with pytest.raises(ValidationError):
        FollowupSuggestions(suggestions=tuple(_suggestion(i) for i in range(1, 5)))
    with pytest.raises(ValidationError):
        FollowupSuggestions(
            suggestions=tuple(_suggestion(1) for _ in range(5))
        )
    with pytest.raises(ValidationError):
        FollowupSuggestion(title="English title", prompt="Please analyze this result")


@pytest.mark.parametrize(
    "value",
    [
        "https://secret.example/api/v1",
        "Bearer token-value",
        "sk-tp-secret",
        "datatap.social.search.v1",
        "step_01",
        "550e8400-e29b-41d4-a716-446655440000",
    ],
)
def test_internal_references_are_rejected_without_echoing_value(value: str) -> None:
    assert contains_internal_reference(value)
    error = safe_followup_error(ValueError(value), stage="validate")
    assert value not in str(error)


def test_request_contains_only_safe_summary_fields() -> None:
    request = build_followup_request(
        user_query="找出最近30天活跃达人",
        session_filters={"category": "餐饮", "platforms": ["douyin"]},
        tool_summary={"抖音": {"succeeded": 1, "failed": 0}},
        candidate_count=10,
        bi_summary={"overview": {"candidate_count": 10}, "analytics_available": ["sentiment"]},
        conclusion="本轮结果显示内容匹配度较高。",
    )
    content = request.messages[-1].content
    assert "找出最近30天活跃达人" in content
    assert "餐饮" in content
    assert "原始" not in content
    assert "sk-" not in content
    assert "datatap" not in content.lower()
    assert request.purpose == "followup"


def test_followup_lock_is_reentrant_safe_for_same_task() -> None:
    from app.tasks.followups import InMemoryFollowupLock

    async def run() -> None:
        lock = InMemoryFollowupLock()
        first = await lock.acquire("task-1")
        second = await lock.acquire("task-1", timeout=0)
        assert first is True
        assert second is False
        await lock.release("task-1")

    asyncio.run(run())
