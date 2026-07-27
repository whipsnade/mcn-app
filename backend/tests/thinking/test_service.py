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
async def test_rebinding_retry_turn_preserves_old_task_blocks_and_explicit_new_task() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    await service.bind_turn(
        turn_id="turn-1",
        user_id="user-1",
        session_id="session-1",
        task_id="source-task",
        trigger_message_id="message-1",
    )
    source = service.create_sink(operation("op-source", "turn-1", "session-1"))
    await source.started(attempt=1)
    await source.completed(attempt=1, duration_ms=1)

    await service.bind_turn(
        turn_id="turn-1",
        user_id="user-1",
        session_id="session-1",
        task_id="retry-task",
        trigger_message_id="message-1",
    )
    retry = service.create_sink(
        operation("op-retry", "turn-1", "session-1", task_id="retry-task")
    )
    await retry.started(attempt=1)
    await retry.completed(attempt=1, duration_ms=1)

    blocks = await service.completed_blocks(
        turn_id="turn-1",
        user_id="user-1",
        session_id="session-1",
    )

    assert [(block.operation_id, block.task_id) for block in blocks] == [
        ("op-source", "source-task"),
        ("op-retry", "retry-task"),
    ]


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

    # 新订阅不再收到运行中快照，但会回放近期终态事件（断线重连收敛）。
    replayed = drain(queue)
    assert [event.type for event in replayed] == ["thinking.failed"]
    assert replayed[0].payload["status"] == "interrupted"
    assert blocks[0].status == "interrupted"


@pytest.mark.asyncio
async def test_resubscribe_replays_terminal_reached_while_disconnected() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=8)
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    await sink.delta("分析中", attempt=1)
    drain(queue)
    await service.unsubscribe("session-1", queue)

    # 断线期间 operation 进入终态。
    await sink.completed(attempt=1, duration_ms=5)

    reconnected = await service.subscribe("session-1")
    replayed = drain(reconnected)
    assert [event.type for event in replayed] == ["thinking.completed"]
    assert replayed[0].payload["operation_id"] == "op-1"
    assert replayed[0].payload["status"] == "completed"


@pytest.mark.asyncio
async def test_terminal_replay_is_bounded_per_session() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=512)
    for index in range(210):
        sink = service.create_sink(
            operation(f"op-{index}", f"turn-{index}", "session-1")
        )
        await sink.started(attempt=1)
        await sink.completed(attempt=1, duration_ms=1)

    queue = await service.subscribe("session-1")
    replayed = drain(queue)

    assert len(replayed) == 200
    assert replayed[0].payload["operation_id"] == "op-10"


@pytest.mark.asyncio
async def test_turn_budget_clamps_running_delta_in_real_time() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=16)
    # 两个已完成 block 各占满 12k，turn 预算剩余约 6k。
    for index in range(2):
        sink = service.create_sink(operation(f"op-{index}", "turn-1", "session-1"))
        await sink.started(attempt=1)
        await sink.delta("甲" * 25_000, attempt=1)
        await sink.completed(attempt=1, duration_ms=1)

    queue = await service.subscribe("session-1")
    drain(queue)
    streaming = service.create_sink(operation("op-2", "turn-1", "session-1"))
    await streaming.started(attempt=1)
    await streaming.delta("乙" * 12_000, attempt=1)

    published = [
        event
        for event in drain(queue)
        if event.type in ("thinking.delta", "thinking.snapshot")
    ]
    assert published
    # 实时 delta 即按 turn 剩余额度收敛，而非等终态才截断。
    latest = published[-1].payload["text"]
    assert len(latest) <= 6_000
    assert latest.startswith("…（早期内容已折叠）")
    assert latest.endswith("乙" * 100)


@pytest.mark.asyncio
async def test_completed_blocks_tracks_persisted_keys() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    first = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await first.started(attempt=1)
    await first.completed(attempt=1, duration_ms=1)

    await service.mark_blocks_persisted(
        turn_id="turn-1",
        user_id="user-1",
        session_id="session-1",
        keys=[("op-1", 1)],
    )
    second = service.create_sink(operation("op-2", "turn-1", "session-1"))
    await second.started(attempt=1)
    await second.completed(attempt=1, duration_ms=1)

    unpersisted = await service.completed_blocks(
        turn_id="turn-1",
        user_id="user-1",
        session_id="session-1",
        only_unpersisted=True,
    )
    all_blocks = await service.completed_blocks(
        turn_id="turn-1", user_id="user-1", session_id="session-1"
    )

    assert [block.operation_id for block in unpersisted] == ["op-2"]
    assert [block.operation_id for block in all_blocks] == ["op-1", "op-2"]
    stranger = await service.completed_blocks(
        turn_id="turn-1",
        user_id="stranger",
        session_id="session-1",
        only_unpersisted=True,
    )
    assert stranger == ()


@pytest.mark.asyncio
async def test_turn_state_is_evicted_beyond_retention_cap() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(max_retained_turns=2)
    for index in range(3):
        sink = service.create_sink(
            operation(f"op-{index}", f"turn-{index}", "session-1")
        )
        await sink.started(attempt=1)
        await sink.completed(attempt=1, duration_ms=1)

    evicted = await service.completed_blocks(
        turn_id="turn-0", user_id="user-1", session_id="session-1"
    )
    retained = await service.completed_blocks(
        turn_id="turn-2", user_id="user-1", session_id="session-1"
    )

    assert evicted == ()
    assert [block.operation_id for block in retained] == ["op-2"]


@pytest.mark.asyncio
async def test_running_turn_is_never_evicted() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(max_retained_turns=2)
    running = service.create_sink(operation("op-running", "turn-0", "session-1"))
    await running.started(attempt=1)
    for index in range(1, 4):
        sink = service.create_sink(
            operation(f"op-{index}", f"turn-{index}", "session-1")
        )
        await sink.started(attempt=1)
        await sink.completed(attempt=1, duration_ms=1)

    await running.delta("仍在分析", attempt=1)
    queue = await service.subscribe("session-1")
    snapshots = [
        event for event in drain(queue) if event.type == "thinking.snapshot"
    ]

    assert [event.payload["operation_id"] for event in snapshots] == ["op-running"]


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
    # 预算不足时折叠最旧已完成块，最新块保持完整。
    assert blocks[0].content == "「早期思考已折叠」"
    assert blocks[0].truncated is True
    assert blocks[-1].content == "2" * 12_000
    assert blocks[-1].truncated is False


@pytest.mark.asyncio
async def test_turn_budget_folds_oldest_completed_blocks_for_new_block() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    for index in range(4):
        sink = service.create_sink(operation(f"op-{index}", "turn-1", "session-1"))
        await sink.started(attempt=1)
        await sink.delta(str(index) * 12_000, attempt=1)
        await sink.completed(attempt=1, duration_ms=1)

    blocks = await service.completed_blocks(
        turn_id="turn-1", user_id="user-1", session_id="session-1"
    )

    # 按完成顺序折叠最旧块，直到新块写入额度足够。
    assert blocks[0].content == "「早期思考已折叠」"
    assert blocks[1].content == "「早期思考已折叠」"
    assert blocks[2].content == "2" * 12_000
    assert blocks[3].content == "3" * 12_000
    assert blocks[3].truncated is False
    assert sum(len(block.content) for block in blocks) <= 30_000

    # 幂等：后续块完成时占位符不被二次折叠或改写。
    fifth = service.create_sink(operation("op-4", "turn-1", "session-1"))
    await fifth.started(attempt=1)
    await fifth.delta("尾" * 100, attempt=1)
    await fifth.completed(attempt=1, duration_ms=1)

    blocks = await service.completed_blocks(
        turn_id="turn-1", user_id="user-1", session_id="session-1"
    )

    assert blocks[0].content == "「早期思考已折叠」"
    assert blocks[1].content == "「早期思考已折叠」"
    assert blocks[4].content == "尾" * 100
    assert sum(len(block.content) for block in blocks) <= 30_000


@pytest.mark.asyncio
async def test_truncated_block_streams_throttled_tail_snapshots() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService(queue_size=16)
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    drain(queue)

    # 单块超 12k：保尾后不再前缀递增，发布整段 snapshot。
    await sink.delta("甲" * 13_000, attempt=1)
    first = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert first.type == "thinking.snapshot"
    assert first.payload["text"].startswith("…（早期内容已折叠）")
    assert first.payload["text"].endswith("甲" * 100)
    assert len(first.payload["text"]) <= 12_000

    # 截断后小额增长（<1000 raw 字符）被节流，不发布新事件。
    await sink.delta("乙" * 500, attempt=1)
    assert queue.empty()

    # 累计增长达到 1000 raw 字符后发布最新尾部快照。
    await sink.delta("丙" * 500, attempt=1)
    second = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert second.type == "thinking.snapshot"
    assert second.payload["text"].startswith("…（早期内容已折叠）")
    assert second.payload["text"].endswith("丙" * 500)

    # 终态块内容始终是最新尾部（节流不影响落库内容）。
    await sink.completed(attempt=1, duration_ms=1)
    blocks = await service.completed_blocks(
        turn_id="turn-1", user_id="user-1", session_id="session-1"
    )
    assert blocks[0].truncated is True
    assert blocks[0].content.startswith("…（早期内容已折叠）")
    assert blocks[0].content.endswith("丙" * 500)
    assert len(blocks[0].content) <= 12_000


@pytest.mark.asyncio
async def test_unsubscribe_stops_future_events() -> None:
    _, SessionThinkingService = thinking_types()
    service = SessionThinkingService()
    queue = await service.subscribe("session-1")
    await service.unsubscribe("session-1", queue)
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))

    await sink.started(attempt=1)

    assert queue.empty()
