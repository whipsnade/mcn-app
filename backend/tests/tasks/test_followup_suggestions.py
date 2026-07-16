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


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 5])
def test_followup_schema_accepts_zero_to_five_unique_chinese_suggestions(count: int) -> None:
    result = FollowupSuggestions(suggestions=tuple(_suggestion(i) for i in range(1, count + 1)))
    assert len(result.suggestions) == count


def test_followup_schema_rejects_more_than_five_or_duplicates() -> None:
    with pytest.raises(ValidationError):
        FollowupSuggestions(suggestions=tuple(_suggestion(i) for i in range(1, 7)))
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


@pytest.mark.parametrize(
    "value",
    [
        "ftp://secret.example/file",
        "file:///tmp/secret.json",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        "get_audience_basic_profile",
        '{"structured_content":{"raw":"secret"}}',
    ],
)
def test_input_safety_redacts_sensitive_values_from_followup_prompt(value: str) -> None:
    request = build_followup_request(
        user_query=value,
        session_filters={"brand": value, "raw_payload": value},
        tool_summary={"抖音": {"succeeded": 1}},
        candidate_count=1,
        bi_summary={},
        conclusion=value,
    )
    content = request.messages[-1].content
    assert value not in content
    assert "raw_payload" not in content
    assert "structured_content" not in content


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


def test_safe_followup_error_uses_whitelisted_code_and_safe_diagnostics() -> None:
    error = safe_followup_error(ValueError("sk-secret"), stage="model")
    assert error["error_code"] == "FOLLOWUP_GENERATION_FAILED"
    assert "sk-secret" not in str(error)


def test_task_read_prefers_persisted_assistant_metadata_fields() -> None:
    from types import SimpleNamespace

    from app.tasks.router import task_read

    task = SimpleNamespace(
        id="task-1",
        session_id="session-1",
        trigger_message_id="user-message",
        status="completed",
        estimated_points=10,
        error_code=None,
        error_message=None,
    )
    result = task_read(
        task,
        {
            "task_id": "task-1",
            "followup_suggestions_status": "completed",
            "followup_suggestions": [_suggestion(1)],
        },
    )
    assert result.followup_suggestions_status == "completed"
    assert result.followup_suggestions[0]["title"].startswith("受众地域")
