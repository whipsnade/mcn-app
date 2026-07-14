from typing import Any

import pytest
from pydantic import ValidationError

from app.model.contracts import ModelRequestMetadata, TokenUsage
from app.model.runs import ModelRunService


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flush_count = 0

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1


def metadata() -> ModelRequestMetadata:
    return ModelRequestMetadata(
        purpose="planner",
        provider="tencent_plan",
        model="deepseek-v4-pro-202606",
        prompt_template="planner_v1",
        prompt_version="1",
    )


@pytest.mark.asyncio
async def test_model_run_records_success_metadata_without_body_or_key() -> None:
    db = FakeSession()
    service = ModelRunService(db)  # type: ignore[arg-type]

    run = await service.start("task-1", metadata())
    await service.succeed(
        run,
        usage=TokenUsage(prompt_tokens=8, completion_tokens=2, total_tokens=10),
        request_id="request-1",
        duration_ms=25,
    )

    assert run.status == "succeeded"
    assert run.total_tokens == 10
    assert run.request_id == "request-1"
    assert "content" not in run.__table__.columns
    assert "api_key" not in run.__table__.columns
    assert db.flush_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["MODEL_PLAN_INVALID", "MODEL_STREAM_INTERRUPTED"])
async def test_model_run_records_structured_and_stream_failures(error_type: str) -> None:
    db = FakeSession()
    service = ModelRunService(db)  # type: ignore[arg-type]
    run = await service.start("task-1", metadata())

    await service.fail(run, error_type=error_type, request_id=None, duration_ms=12)

    assert run.status == "failed"
    assert run.error_type == error_type
    assert run.duration_ms == 12


def test_model_run_metadata_rejects_prompt_body_and_secret() -> None:
    with pytest.raises(ValidationError):
        ModelRequestMetadata(
            purpose="planner",
            provider="tencent_plan",
            model="deepseek-v4-pro-202606",
            prompt_template="planner_v1",
            prompt_version="1",
            content="sensitive body",
            api_key="secret",
        )
