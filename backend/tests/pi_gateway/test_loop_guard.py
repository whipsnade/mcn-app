"""跨 Attempt loop guard 的持久化契约。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentMessage
from app.agent_runtime.tools.contracts import ToolResult
from app.pi_gateway.loop_guard import (
    AGENT_LOOP_CIRCUIT_OPEN,
    BUILDER_GUARD_THRESHOLD,
    LoopGuard,
    error_fingerprint,
)

from .test_model_usage import _run


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _builder_error(summary: str = "missing evidence at 2026-08-12T12:00:00Z") -> ToolResult:
    return ToolResult(
        status="failed",
        safe_summary=summary,
        error_type="draft_build_error",
    )


@pytest.mark.asyncio
async def test_builder_guard_opens_on_third_identical_error_and_persists_explanation(
    db_session, user_factory
) -> None:
    user = await user_factory()
    run, _attempt, _tenant_id = await _run(db_session, user)
    guard = LoopGuard(db_session)

    first = await guard.record_builder_result(run, "build_brand_report_draft", _builder_error())
    second = await guard.record_builder_result(run, "build_brand_report_draft", _builder_error())
    opened = await guard.record_builder_result(run, "build_brand_report_draft", _builder_error())

    assert first.error_type == "draft_build_error"
    assert second.error_type == "draft_build_error"
    assert opened.error_type == AGENT_LOOP_CIRCUIT_OPEN
    assert opened.status == "failed"
    assert run.loop_guard_json["terminal_code"] == AGENT_LOOP_CIRCUIT_OPEN
    assert run.loop_guard_json["builder"]["streak"] == BUILDER_GUARD_THRESHOLD
    messages = list(
        (
            await db_session.scalars(
                select(AgentMessage).where(
                    AgentMessage.run_id == run.id,
                    AgentMessage.metadata_json["system_loop_guard"].as_boolean().is_(True),
                )
            )
        ).all()
    )
    assert len(messages) == 1

    blocked = await guard.reject_if_open(run)
    assert blocked is not None
    assert blocked.error_type == AGENT_LOOP_CIRCUIT_OPEN


@pytest.mark.asyncio
async def test_loop_guard_is_not_reset_by_a_new_attempt(db_session, user_factory) -> None:
    user = await user_factory()
    run, _attempt, _tenant_id = await _run(db_session, user)
    guard = LoopGuard(db_session)
    for _ in range(BUILDER_GUARD_THRESHOLD):
        await guard.record_builder_result(run, "build_brand_report_draft", _builder_error())

    run.loop_guard_json = dict(run.loop_guard_json)
    run.loop_guard_json["attempt_marker_written_by_test"] = 2
    await db_session.flush()

    blocked = await guard.reject_if_open(run)

    assert blocked is not None
    assert run.loop_guard_json["terminal_code"] == AGENT_LOOP_CIRCUIT_OPEN


@pytest.mark.asyncio
async def test_search_guard_opens_when_evidence_set_and_result_do_not_change(
    db_session, user_factory
) -> None:
    user = await user_factory()
    run, _attempt, _tenant_id = await _run(db_session, user)
    guard = LoopGuard(db_session)
    args = {"query": "品牌", "cursor": None, "filters": None}
    result = ToolResult(status="success", safe_summary='{"matches":[],"has_more":false}')

    for _ in range(2):
        recorded = await guard.record_search_result(run, args, result)
        assert recorded.error_type is None
    opened = await guard.record_search_result(run, args, result)

    assert opened.error_type == AGENT_LOOP_CIRCUIT_OPEN
    assert run.loop_guard_json["search_evidence"]["streak"] == 3
    assert isinstance(run.loop_guard_json["search_evidence"]["evidence_set_version"], str)


def test_error_fingerprint_is_stable_when_uuid_and_timestamp_change() -> None:
    a = error_fingerprint(
        "build_brand_report_draft",
        "draft_build_error",
        "evidence 11111111-1111-4111-8111-111111111111 failed at 2026-08-12T12:00:00Z",
    )
    b = error_fingerprint(
        "build_brand_report_draft",
        "draft_build_error",
        "evidence 22222222-2222-4222-8222-222222222222 failed at 2027-01-01T00:00:00Z",
    )

    assert a == b
