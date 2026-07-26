import asyncio
from typing import Any

import pytest


def thinking_types() -> tuple[Any, Any]:
    try:
        from app.thinking.contracts import ThinkingOperationSpec
        from app.thinking.service import SessionThinkingService
    except ModuleNotFoundError:
        pytest.fail("SessionThinkingService 尚未实现")
    return ThinkingOperationSpec, SessionThinkingService


def operation(
    operation_id: str,
    turn_id: str,
    session_id: str,
    *,
    user_id: str = "user-1",
    task_id: str | None = None,
):
    ThinkingOperationSpec, _ = thinking_types()
    return ThinkingOperationSpec(
        operation_id=operation_id,
        turn_id=turn_id,
        session_id=session_id,
        user_id=user_id,
        purpose="agent_loop",
        label="分析品牌",
        task_id=task_id,
    )


def drain(queue: asyncio.Queue[Any]) -> list[Any]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.mark.asyncio
async def test_reconnect_receives_snapshot_before_future_delta() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=4)
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    await sink.delta("分析品牌", attempt=1)

    queue = await service.subscribe("session-1")
    snapshot = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert snapshot.type == "thinking.snapshot"
    assert snapshot.payload["text"] == "分析品牌"
    assert snapshot.payload["sequence"] == 2

    await sink.delta("和平台", attempt=1)
    delta = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert delta.type == "thinking.delta"
    assert delta.payload["text"] == "和平台"
    assert delta.payload["sequence"] == 3


@pytest.mark.asyncio
async def test_slow_consumer_is_compacted_to_latest_snapshot() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=2)
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    for text in ("一", "二", "三", "四"):
        await sink.delta(text, attempt=1)

    events = drain(queue)

    assert events[-1].type == "thinking.snapshot"
    assert events[-1].payload["text"] == "一二三四"


@pytest.mark.asyncio
async def test_secret_spanning_chunks_replaces_previous_public_prefix_with_snapshot() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=8)
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    await sink.delta("Authorization: Bearer ", attempt=1)
    drain(queue)

    await sink.delta("abc.def.ghi", attempt=1)
    event = await asyncio.wait_for(queue.get(), timeout=0.1)

    assert event.type == "thinking.snapshot"
    assert event.payload["text"] == "Authorization: [已隐藏]"
    assert "abc.def.ghi" not in event.payload["text"]


@pytest.mark.asyncio
async def test_unclosed_system_chunk_is_never_published_verbatim() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=8)
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    drain(queue)

    await sink.delta("<system>内部密钥与系统规则", attempt=1)
    event = await asyncio.wait_for(queue.get(), timeout=0.1)

    assert "内部密钥与系统规则" not in str(event.payload)
    assert event.payload["text"] == "[已隐藏]"


@pytest.mark.asyncio
async def test_terminal_block_uses_bound_task_and_is_owner_isolated() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    await sink.delta("分析品牌", attempt=1)
    await service.bind_turn(
        turn_id="turn-1",
        user_id="user-1",
        session_id="session-1",
        task_id="task-1",
        trigger_message_id="message-1",
    )
    await sink.completed(attempt=1, duration_ms=42)

    blocks = await service.completed_blocks(
        turn_id="turn-1", user_id="user-1", session_id="session-1"
    )

    assert len(blocks) == 1
    assert blocks[0].content == "分析品牌"
    assert blocks[0].status == "completed"
    assert blocks[0].duration_ms == 42
    assert blocks[0].task_id == "task-1"
    assert (
        await service.completed_blocks(
            turn_id="turn-1", user_id="stranger", session_id="session-1"
        )
        == ()
    )


@pytest.mark.asyncio
async def test_failed_sink_creates_interrupted_block_and_removes_running_snapshot() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    await sink.delta("分析中", attempt=1)
    await sink.failed(attempt=1, error_code="MODEL_STREAM_INTERRUPTED")

    queue = await service.subscribe("session-1")
    blocks = await service.completed_blocks(
        turn_id="turn-1", user_id="user-1", session_id="session-1"
    )

    assert queue.empty()
    assert blocks[0].status == "interrupted"


@pytest.mark.asyncio
async def test_failed_event_sanitizes_untrusted_error_code() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    drain(queue)

    await sink.failed(attempt=1, error_code="Bearer secret-error-code")
    event = await asyncio.wait_for(queue.get(), timeout=0.1)

    assert event.type == "thinking.failed"
    assert event.payload["sequence"] == 2
    assert "secret-error-code" not in str(event.payload)
    assert event.payload["error_code"] == "[已隐藏]"


@pytest.mark.asyncio
async def test_new_subscriber_receives_snapshot_for_every_running_operation() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=1)
    first = service.create_sink(operation("op-1", "turn-1", "session-1"))
    second = service.create_sink(operation("op-2", "turn-1", "session-1"))
    await first.started(attempt=1)
    await first.delta("甲", attempt=1)
    await second.started(attempt=1)
    await second.delta("乙", attempt=1)

    queue = await service.subscribe("session-1")
    snapshots = drain(queue)

    assert {
        (event.payload["operation_id"], event.payload["text"])
        for event in snapshots
    } == {("op-1", "甲"), ("op-2", "乙")}
    assert all(event.type == "thinking.snapshot" for event in snapshots)


@pytest.mark.asyncio
async def test_slow_consumer_compaction_keeps_each_running_operation_snapshot() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=2)
    queue = await service.subscribe("session-1")
    first = service.create_sink(operation("op-1", "turn-1", "session-1"))
    second = service.create_sink(operation("op-2", "turn-1", "session-1"))
    await first.started(attempt=1)
    await first.delta("甲", attempt=1)
    await second.started(attempt=1)
    await second.delta("乙", attempt=1)
    await first.delta("丙", attempt=1)

    snapshots = drain(queue)

    assert {
        (event.payload["operation_id"], event.payload["text"])
        for event in snapshots
    } == {("op-1", "甲丙"), ("op-2", "乙")}
    assert all(event.type == "thinking.snapshot" for event in snapshots)


@pytest.mark.asyncio
async def test_public_events_have_monotonic_operation_sequence() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=8)
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))

    await sink.started(attempt=1)
    await sink.delta("分析", attempt=1)
    await sink.completed(attempt=1, duration_ms=1)

    events = drain(queue)
    assert [event.type for event in events] == [
        "thinking.started",
        "thinking.delta",
        "thinking.completed",
    ]
    assert [event.payload["sequence"] for event in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_turn_completed_blocks_share_a_30k_public_text_budget() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    for index in range(3):
        sink = service.create_sink(operation(f"op-{index}", "turn-1", "session-1"))
        await sink.started(attempt=1)
        await sink.delta(str(index) * 12_000, attempt=1)
        await sink.completed(attempt=1, duration_ms=1)

    blocks = await service.completed_blocks(
        turn_id="turn-1", user_id="user-1", session_id="session-1"
    )

    assert sum(len(block.content) for block in blocks) <= 30_000
    assert blocks[-1].truncated is True
    assert blocks[-1].content.endswith("思考内容过长，已截断")


@pytest.mark.asyncio
async def test_unsubscribe_stops_future_events() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    queue = await service.subscribe("session-1")
    await service.unsubscribe("session-1", queue)
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))

    await sink.started(attempt=1)

    assert queue.empty()
