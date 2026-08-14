"""Task 8D：同一 Pi Run 的 RPC 审计与 DataTap 审计必须共用锁序。"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError

from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import RunStatus
from app.core.config import get_settings
from app.db.session import SessionFactory
from app.identity.models import User, UserChannelPermission
from app.pi_runtime_poc.audit import PiRunAuditWriter
from app.pi_runtime_poc.auth import issue_run_token
from app.pi_runtime_poc.comparison import PocCase, PocCaseFactory
from app.pi_runtime_poc.schemas import PiToolFailed, PiToolSettled, PiToolStarted
from app.pi_runtime_poc.service import PiEvidenceIngestService

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PI_POC_MYSQL_TESTS") != "1",
    reason="仅在显式隔离 kol_insight_pi_poc MySQL 验收中执行",
)


async def _delete_poc_run(run_id: str) -> None:
    """删除本文件创建的严格隔离 POC fixture。"""
    async with SessionFactory() as db:
        run = await db.get(AgentRun, run_id)
        if run is None:
            return
        user_id, session_id = run.user_id, run.session_id
        run.input_message_id = None
        await db.flush()
        await db.execute(delete(EvidenceItem).where(EvidenceItem.run_id == run_id))
        await db.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
        await db.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
        await db.execute(delete(AgentStep).where(AgentStep.run_id == run_id))
        await db.execute(delete(AgentMessage).where(AgentMessage.run_id == run_id))
        await db.execute(delete(AgentRunAttempt).where(AgentRunAttempt.run_id == run_id))
        await db.execute(delete(AgentRun).where(AgentRun.id == run_id))
        await db.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await db.execute(delete(UserChannelPermission).where(UserChannelPermission.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_long_lived_runner_reads_current_event_sequence_after_extension_commit() -> None:
    """旧 Runner 快照不能遮蔽 Extension 已提交的工具产品事件。"""
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"

    case = PocCase(
        case_id="task8e-stale-event-snapshot",
        user_question="只验证长事务中的 Event 当前读。",
        date_anchor="2026-08-08",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(
        SessionFactory,
        round_id="task8e-stale-event-snapshot",
        model_name=settings.tencent_plan_model,
    )
    run_id = await factory.create(case)

    try:
        async with SessionFactory() as runner_db:
            run = await runner_db.get(AgentRun, run_id)
            assert run is not None
            assert (
                await runner_db.scalar(
                    select(func.count())
                    .select_from(AgentEvent)
                    .where(AgentEvent.run_id == run_id)
                )
            ) == 0

            # Extension HTTP 的独立短事务实际写入 tool.started/tool.succeeded。
            async with SessionFactory() as extension_db:
                extension_writer = PiRunAuditWriter(
                    db=extension_db,
                    events=AgentEventStream(extension_db, AgentEventBroker()),
                )
                call_id = await extension_writer.start_tool(
                    run_id=run_id,
                    pi_call_id="task8e-stale-event-call",
                    tool_name="task8e_schema_probe",
                    arguments={"source": "mysql"},
                )
                await extension_writer.settle_tool(
                    run_id=run_id,
                    call_id=call_id,
                    raw_payload={"rows": [{"id": 1}]},
                )

            runner_writer = PiRunAuditWriter(
                db=runner_db,
                events=AgentEventStream(runner_db, AgentEventBroker()),
            )
            await runner_writer.write_rpc_event(run_id=run_id, event={"type": "agent_start"})

        async with SessionFactory() as db:
            events = list(
                (
                    await db.execute(
                        select(AgentEvent.sequence, AgentEvent.event_type)
                        .where(AgentEvent.run_id == run_id)
                        .order_by(AgentEvent.sequence)
                    )
                ).all()
            )
        assert [sequence for sequence, _ in events] == list(range(1, len(events) + 1))
        assert [event_type for _, event_type in events] == [
            "tool.started",
            "tool.succeeded",
            "thinking.started",
        ]
    finally:
        await _delete_poc_run(run_id)


async def test_begin_attempt_uses_current_max_after_other_session_commit() -> None:
    """已建立快照的恢复流程必须看见另一事务已提交的 Attempt。"""
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"

    case = PocCase(
        case_id="task8e-stale-attempt-snapshot",
        user_question="只验证长事务中的 Attempt 当前读。",
        date_anchor="2026-08-08",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(
        SessionFactory,
        round_id="task8e-stale-attempt-snapshot",
        model_name=settings.tencent_plan_model,
    )
    run_id = await factory.create(case)

    try:
        async with SessionFactory() as runner_db:
            assert (
                await runner_db.scalar(
                    select(func.count())
                    .select_from(AgentRunAttempt)
                    .where(AgentRunAttempt.run_id == run_id)
                )
            ) == 0

            async with SessionFactory() as extension_db:
                extension_repo = AgentRunRepository(extension_db)
                first_attempt = await extension_repo.begin_attempt(run_id)
                run = await extension_db.get(AgentRun, run_id)
                assert run is not None
                run.status = RunStatus.PAUSED
                run.paused_at = datetime.now(UTC).replace(tzinfo=None)
                first_attempt.outcome = "paused"
                first_attempt.ended_at = datetime.now(UTC).replace(tzinfo=None)
                await extension_db.commit()

            resumed_attempt = await AgentRunRepository(runner_db).begin_attempt(
                run_id,
                resumed=True,
            )
            assert resumed_attempt.attempt == 2
            await runner_db.commit()

        async with SessionFactory() as db:
            attempts = list(
                (
                    await db.scalars(
                        select(AgentRunAttempt.attempt)
                        .where(AgentRunAttempt.run_id == run_id)
                        .order_by(AgentRunAttempt.attempt)
                    )
                ).all()
            )
        assert attempts == [1, 2]
    finally:
        await _delete_poc_run(run_id)


async def test_next_step_sequence_uses_current_max_after_other_session_commit() -> None:
    """已建立快照的 Engine 必须从当前 Step 序号继续分配。"""
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"

    case = PocCase(
        case_id="task8e-stale-engine-step-snapshot",
        user_question="只验证 Engine Step 当前读。",
        date_anchor="2026-08-08",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(
        SessionFactory,
        round_id="task8e-stale-engine-step-snapshot",
        model_name=settings.tencent_plan_model,
    )
    run_id = await factory.create(case)

    try:
        async with SessionFactory() as runner_db:
            assert (
                await runner_db.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(AgentStep.run_id == run_id)
                )
            ) == 0

            async with SessionFactory() as extension_db:
                attempt = AgentRunAttempt(
                    id=str(uuid4()),
                    run_id=run_id,
                    attempt=1,
                    started_at=datetime.now(UTC).replace(tzinfo=None),
                    decision_count=0,
                    outcome="running",
                )
                extension_db.add(attempt)
                await extension_db.flush()
                extension_db.add(
                    AgentStep(
                        id=str(uuid4()),
                        run_id=run_id,
                        attempt_id=attempt.id,
                        sequence=1,
                        step_type="pi_rpc_event",
                        input_json={},
                        output_json=None,
                        status="completed",
                        visibility="internal",
                        created_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                await extension_db.commit()

            engine = object.__new__(AgentEngine)
            engine._db = runner_db
            assert await engine._next_step_sequence(run_id) == 2
            await runner_db.commit()
    finally:
        await _delete_poc_run(run_id)


async def test_long_lived_runner_reads_current_step_sequence_after_extension_commit() -> None:
    """Runner 的旧快照不能遮蔽 Extension 已提交的 Step sequence。"""
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"

    case = PocCase(
        case_id="task8d-stale-step-snapshot",
        user_question="只验证长事务中的 Step 当前读。",
        date_anchor="2026-08-08",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(
        SessionFactory,
        round_id="task8d-stale-step-snapshot",
        model_name=settings.tencent_plan_model,
    )
    run_id = await factory.create(case)
    user_id: str | None = None

    try:
        async with SessionFactory() as runner_db:
            run = await runner_db.get(AgentRun, run_id)
            assert run is not None
            user_id = run.user_id

            # 先在 Runner 的长会话中建立 REPEATABLE-READ 一致性快照。
            assert (
                await runner_db.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(AgentStep.run_id == run_id)
                )
            ) == 0

            # Extension HTTP 请求使用独立会话提交 sequence=1。
            async with SessionFactory() as extension_db:
                extension_writer = PiRunAuditWriter(
                    db=extension_db,
                    events=AgentEventStream(extension_db, AgentEventBroker()),
                )
                await extension_writer.write_extension_diagnostic(
                    run_id=run_id,
                    diagnostic={
                        "code": "pi_extension_stage",
                        "stage": "audit_start",
                        "service_slug": "insight-cube-mcp",
                        "tool_name": None,
                        "exception_type": None,
                    },
                )

            runner_writer = PiRunAuditWriter(
                db=runner_db,
                events=AgentEventStream(runner_db, AgentEventBroker()),
            )
            await runner_writer.write_rpc_event(
                run_id=run_id,
                event={"type": "agent_start"},
            )

        async with SessionFactory() as db:
            steps = list(
                (
                    await db.execute(
                        select(AgentStep.sequence, AgentStep.step_type)
                        .where(AgentStep.run_id == run_id)
                        .order_by(AgentStep.sequence)
                    )
                ).all()
            )
        assert steps == [
            (1, "pi_extension_diagnostic"),
            (2, "pi_rpc_event"),
        ]
    finally:
        if user_id is not None:
            async with SessionFactory() as db:
                run = await db.get(AgentRun, run_id)
                if run is not None:
                    run.input_message_id = None
                    await db.flush()
                await db.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
                await db.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
                await db.execute(delete(AgentStep).where(AgentStep.run_id == run_id))
                await db.execute(delete(AgentMessage).where(AgentMessage.run_id == run_id))
                await db.execute(delete(AgentRun).where(AgentRun.id == run_id))
                await db.execute(delete(AgentSession).where(AgentSession.user_id == user_id))
                await db.execute(
                    delete(UserChannelPermission).where(
                        UserChannelPermission.user_id == user_id
                    )
                )
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()


async def test_same_run_rpc_audit_and_tool_start_are_serialized_in_mysql() -> None:
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"

    case = PocCase(
        case_id="task8d-audit-lock-order",
        user_question="只验证同一 Run 的审计锁顺序。",
        date_anchor="2026-08-07",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(
        SessionFactory,
        round_id="task8d-audit",
        model_name=settings.tencent_plan_model,
    )
    run_id = await factory.create(case)
    user_id: str | None = None
    barrier = asyncio.Barrier(2)

    try:
        async with SessionFactory() as db:
            run = await db.get(AgentRun, run_id)
            assert run is not None
            user_id = run.user_id

        async def write_rpc_event() -> None:
            async with SessionFactory() as db:
                writer = PiRunAuditWriter(
                    db=db,
                    events=AgentEventStream(db, AgentEventBroker()),
                )
                await barrier.wait()
                await writer.write_rpc_event(
                    run_id=run_id,
                    event={"type": "agent_start"},
                )

        async def start_tool() -> None:
            async with SessionFactory() as db:
                service = PiEvidenceIngestService(
                    db=db,
                    events=AgentEventStream(db, AgentEventBroker()),
                    settings=settings,
                )
                await barrier.wait()
                await service.start_tool(
                    token=issue_run_token(run_id, settings=settings),
                    run_id=run_id,
                    request=PiToolStarted(
                        call_id="task8d-concurrent-call",
                        tool_name="task8d_schema_probe",
                        arguments={"source": "mysql"},
                    ),
                )

        results = await asyncio.gather(write_rpc_event(), start_tool(), return_exceptions=True)
        assert not any(isinstance(result, OperationalError) for result in results)
        assert results == [None, None]

        async with SessionFactory() as db:
            step_sequences = list(
                (
                    await db.scalars(
                        select(AgentStep.sequence)
                        .where(AgentStep.run_id == run_id)
                        .order_by(AgentStep.sequence)
                    )
                ).all()
            )
            event_sequences = list(
                (
                    await db.scalars(
                        select(AgentEvent.sequence)
                        .where(AgentEvent.run_id == run_id)
                        .order_by(AgentEvent.sequence)
                    )
                ).all()
            )
            calls = list(
                (
                    await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id))
                ).all()
            )

        assert step_sequences == sorted(set(step_sequences))
        assert event_sequences == sorted(set(event_sequences))
        assert [(call.points_reserved, call.points_settled) for call in calls] == [(0, 0)]
    finally:
        if user_id is not None:
            async with SessionFactory() as db:
                run = await db.get(AgentRun, run_id)
                if run is not None:
                    run.input_message_id = None
                    await db.flush()
                await db.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
                await db.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
                await db.execute(delete(AgentStep).where(AgentStep.run_id == run_id))
                await db.execute(delete(AgentMessage).where(AgentMessage.run_id == run_id))
                await db.execute(delete(AgentRun).where(AgentRun.id == run_id))
                await db.execute(delete(AgentSession).where(AgentSession.user_id == user_id))
                await db.execute(delete(UserChannelPermission).where(UserChannelPermission.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()


@pytest.mark.parametrize(
    ("terminal", "expected_status", "expects_evidence"),
    [
        ("settle", "settled", True),
        ("fail", "failed", False),
    ],
)
async def test_same_run_rpc_audit_and_tool_terminal_are_serialized_in_mysql(
    terminal: str, expected_status: str, expects_evidence: bool
) -> None:
    """settle/fail 也必须先锁 Run，不能在写子表后再反向等待 Run 锁。"""
    settings = get_settings()
    assert settings.app_env == "test"
    assert settings.mysql_database == "kol_insight_pi_poc"

    case = PocCase(
        case_id=f"task8d-audit-{terminal}",
        user_question="只验证同一 Run 的终态审计锁顺序。",
        date_anchor="2026-08-07",
        expected_behavior="clarify",
        required_artifact_type=None,
    )
    factory = PocCaseFactory(SessionFactory, round_id="task8d-audit", model_name=settings.tencent_plan_model)
    run_id = await factory.create(case)
    user_id: str | None = None
    barrier = asyncio.Barrier(2)
    try:
        async with SessionFactory() as db:
            run = await db.get(AgentRun, run_id)
            assert run is not None
            user_id = run.user_id
            service = PiEvidenceIngestService(
                db=db,
                events=AgentEventStream(db, AgentEventBroker()),
                settings=settings,
            )
            started = await service.start_tool(
                token=issue_run_token(run_id, settings=settings),
                run_id=run_id,
                request=PiToolStarted(
                    call_id=f"task8d-{terminal}-call",
                    tool_name="task8d_schema_probe",
                    arguments={"source": "mysql"},
                ),
            )

        async def write_rpc_event() -> None:
            async with SessionFactory() as db:
                writer = PiRunAuditWriter(db=db, events=AgentEventStream(db, AgentEventBroker()))
                await barrier.wait()
                await writer.write_rpc_event(run_id=run_id, event={"type": "agent_end"})

        async def terminal_tool() -> None:
            async with SessionFactory() as db:
                service = PiEvidenceIngestService(
                    db=db,
                    events=AgentEventStream(db, AgentEventBroker()),
                    settings=settings,
                )
                await barrier.wait()
                if terminal == "settle":
                    await service.settle_tool(
                        token=issue_run_token(run_id, settings=settings),
                        run_id=run_id,
                        call_id=started.call_id,
                        request=PiToolSettled(raw_payload={"rows": [{"id": 1}]}),
                    )
                else:
                    await service.fail_tool(
                        token=issue_run_token(run_id, settings=settings),
                        run_id=run_id,
                        call_id=started.call_id,
                        request=PiToolFailed(error={"code": "fake_timeout"}, status="failed"),
                    )

        results = await asyncio.gather(write_rpc_event(), terminal_tool(), return_exceptions=True)
        assert not any(isinstance(result, OperationalError) for result in results)
        assert results == [None, None]

        async with SessionFactory() as db:
            call = await db.get(AgentToolCall, started.call_id)
            assert call is not None
            evidence = list(
                (await db.scalars(select(EvidenceItem).where(EvidenceItem.tool_call_id == started.call_id))).all()
            )
            sequences = list(
                (await db.scalars(select(AgentEvent.sequence).where(AgentEvent.run_id == run_id))).all()
            )
        assert call.status == expected_status
        assert bool(evidence) is expects_evidence
        assert sequences == sorted(set(sequences))
    finally:
        if user_id is not None:
            async with SessionFactory() as db:
                run = await db.get(AgentRun, run_id)
                if run is not None:
                    run.input_message_id = None
                    await db.flush()
                await db.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
                await db.execute(delete(EvidenceItem).where(EvidenceItem.run_id == run_id))
                await db.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run_id))
                await db.execute(delete(AgentStep).where(AgentStep.run_id == run_id))
                await db.execute(delete(AgentMessage).where(AgentMessage.run_id == run_id))
                await db.execute(delete(AgentRun).where(AgentRun.id == run_id))
                await db.execute(delete(AgentSession).where(AgentSession.user_id == user_id))
                await db.execute(delete(UserChannelPermission).where(UserChannelPermission.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
