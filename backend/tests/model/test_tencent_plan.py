import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict, field_validator

from app.model.contracts import (
    ChatMessage,
    ModelAdapterError,
    ModelPlanInvalidError,
    StructuredModelRequest,
)
from app.model.tencent_plan import TencentPlanAdapter
from .fakes import (
    FakeChatCompletions,
    completion,
    connection_error,
    status_error,
    unsupported_schema_error,
)


class MinimalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    steps: list[str]


SENSITIVE_MARKER = "SENSITIVE_VALIDATOR_MARKER_DO_NOT_LEAK"
VALIDATOR_MESSAGE = "validator-rejected-sensitive-input"


class SensitiveValidatorPlan(BaseModel):
    objective: str
    steps: list[str]

    @field_validator("objective")
    @classmethod
    def reject_sensitive_marker(cls, value: str) -> str:
        if SENSITIVE_MARKER in value:
            raise ValueError(f"{VALIDATOR_MESSAGE}: {value!r}")
        return value


def planner_request() -> StructuredModelRequest[MinimalPlan]:
    return StructuredModelRequest(
        purpose="planner",
        template_name="planner_v1",
        messages=(ChatMessage(role="user", content="选人"),),
        output_model=MinimalPlan,
    )


@pytest.mark.asyncio
async def test_json_schema_unsupported_falls_back_to_json_object() -> None:
    client = FakeChatCompletions(
        [unsupported_schema_error(), completion('{"objective":"选人","steps":[]}')]
    )
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock(), schema_support_cache={})

    result = await adapter.complete_json(planner_request())

    assert result.value.objective == "选人"
    assert [call["response_format"]["type"] for call in client.calls] == [
        "json_schema",
        "json_object",
    ]


@pytest.mark.asyncio
async def test_explicit_unsupported_response_format_code_falls_back_without_param() -> None:
    client = FakeChatCompletions(
        [
            unsupported_schema_error(include_param=False),
            completion('{"objective":"选人","steps":[]}'),
        ]
    )
    adapter = TencentPlanAdapter(client=client, schema_support_cache={})

    result = await adapter.complete_json(planner_request())

    assert result.value.objective == "选人"
    assert [call["response_format"]["type"] for call in client.calls] == [
        "json_schema",
        "json_object",
    ]


@pytest.mark.asyncio
async def test_supported_json_schema_is_validated_with_pydantic() -> None:
    client = FakeChatCompletions(
        [completion('{"objective":"选人","steps":[]}', prompt_tokens=8, total_tokens=10)]
    )
    adapter = TencentPlanAdapter(client=client, schema_support_cache={})

    result = await adapter.complete_json(planner_request())

    assert result.value == MinimalPlan(objective="选人", steps=[])
    assert result.usage is not None and result.usage.prompt_tokens == 8
    assert result.request_id == "request-test"
    assert client.calls[0]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_schema_fallback_is_cached_by_deployment_and_schema() -> None:
    cache: dict[tuple[str, str, str], bool] = {}
    first = FakeChatCompletions(
        [unsupported_schema_error(), completion('{"objective":"一","steps":[]}')]
    )
    second = FakeChatCompletions([completion('{"objective":"二","steps":[]}')])

    await TencentPlanAdapter(client=first, schema_support_cache=cache).complete_json(
        planner_request()
    )
    await TencentPlanAdapter(client=second, schema_support_cache=cache).complete_json(
        planner_request()
    )

    assert second.calls[0]["response_format"]["type"] == "json_object"


@pytest.mark.asyncio
async def test_invalid_structure_is_regenerated_only_once() -> None:
    client = FakeChatCompletions(
        [completion("not-json"), completion('{"objective":"选人","steps":"invalid"}')]
    )
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock(), schema_support_cache={})

    with pytest.raises(ModelPlanInvalidError) as caught:
        await adapter.complete_json(planner_request())

    assert caught.value.code == "MODEL_PLAN_INVALID"
    assert len(client.calls) == 2
    assert "validation" in client.calls[1]["messages"][-1]["content"].lower()


@pytest.mark.asyncio
async def test_repair_message_excludes_sensitive_validator_error_details() -> None:
    client = FakeChatCompletions(
        [
            completion(
                '{"objective":"'
                + SENSITIVE_MARKER
                + '","steps":[]}'
            ),
            completion('{"objective":"安全结果","steps":[]}'),
        ]
    )
    request = StructuredModelRequest(
        purpose="planner",
        template_name="planner_v1",
        messages=(ChatMessage(role="user", content="选人"),),
        output_model=SensitiveValidatorPlan,
    )
    adapter = TencentPlanAdapter(client=client, schema_support_cache={})

    result = await adapter.complete_json(request)

    assert result.value.objective == "安全结果"
    assert len(client.calls) == 2
    repair_content = client.calls[1]["messages"][-1]["content"]
    assert SENSITIVE_MARKER not in repair_content
    assert VALIDATOR_MESSAGE not in repair_content
    assert "ValueError" not in repair_content
    assert '"type":"value_error"' in repair_content
    assert '"loc":["objective"]' in repair_content


@pytest.mark.asyncio
async def test_transient_connection_error_has_bounded_retry() -> None:
    sleep = AsyncMock()
    client = FakeChatCompletions(
        [connection_error(), completion('{"objective":"选人","steps":[]}')]
    )
    adapter = TencentPlanAdapter(
        client=client, sleep=sleep, jitter=lambda: 0.0, schema_support_cache={}
    )

    result = await adapter.complete_json(planner_request())

    assert result.value.objective == "选人"
    assert len(client.calls) == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
async def test_retryable_statuses_are_retried(status_code: int) -> None:
    client = FakeChatCompletions(
        [status_error(status_code), completion('{"objective":"选人","steps":[]}')]
    )
    adapter = TencentPlanAdapter(
        client=client,
        sleep=AsyncMock(),
        jitter=lambda: 0.0,
        schema_support_cache={},
    )

    await adapter.complete_json(planner_request())

    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 402, 403, 451, 499])
async def test_non_retryable_statuses_do_not_retry_or_downgrade(status_code: int) -> None:
    client = FakeChatCompletions([status_error(status_code)])
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock(), schema_support_cache={})

    with pytest.raises(ModelAdapterError) as caught:
        await adapter.complete_json(planner_request())

    assert caught.value.retryable is False
    assert len(client.calls) == 1
    assert client.calls[0]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_business_timeout_is_not_retried() -> None:
    client = FakeChatCompletions([asyncio.TimeoutError()])
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock(), schema_support_cache={})

    with pytest.raises(ModelAdapterError) as caught:
        await adapter.complete_json(planner_request())

    assert caught.value.code == "MODEL_TIMEOUT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_structured_cancelled_error_propagates_unchanged() -> None:
    client = FakeChatCompletions([asyncio.CancelledError()])
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock(), schema_support_cache={})

    with pytest.raises(asyncio.CancelledError):
        await adapter.complete_json(planner_request())

    assert len(client.calls) == 1
