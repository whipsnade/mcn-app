from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.thinking.contracts import ThinkingBlock
from app.thinking.persistence import (
    ThinkingMessageStore,
    persist_turn_thinking,
    record_brainstorm_failure,
)
from app.workspace.models import Message, WorkspaceSession


def _block(
    *,
    turn_id: str = "8a9fda07-77c5-44ea-967e-a17e795266ef",
    operation_id: str = "op-1",
    attempt: int = 1,
    status: str = "completed",
    task_id: str | None = None,
) -> ThinkingBlock:
    started_at = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    return ThinkingBlock(
        operation_id=operation_id,
        turn_id=turn_id,
        purpose="brainstorm",
        attempt=attempt,
        label="理解需求",
        content="分析品牌",
        status=status,
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=2),
        duration_ms=2000,
        task_id=task_id,
    )


async def _seed_turn(
    db_session,
    user_factory,
    *,
    turn_id: str = "8a9fda07-77c5-44ea-967e-a17e795266ef",
    assistant_metadata: dict | None = None,
) -> tuple:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="思考持久化测试",
        brand="",
        campaign_name=None,
        status="active",
        platforms=[],
        category=None,
        target_audience="",
        budget_min=None,
        budget_max=None,
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    user_message = Message(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        role="user",
        content="分析品牌",
        sequence=1,
        metadata_json={"turn_id": turn_id},
        created_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add(user_message)
    assistant_message = None
    if assistant_metadata is not None:
        assistant_message = Message(
            id=str(uuid4()),
            session_id=session.id,
            user_id=user.id,
            role="assistant",
            content="分析结论",
            sequence=2,
            metadata_json=assistant_metadata,
            created_at=now,
        )
        db_session.add(assistant_message)
    await db_session.flush()
    return user, session, user_message, assistant_message


@pytest.mark.asyncio
async def test_persist_block_stages_on_user_message_and_deduplicates_replay(
    db_session, user_factory
) -> None:
    user, session, user_message, _ = await _seed_turn(db_session, user_factory)
    block = _block()
    store = ThinkingMessageStore(db_session)

    await store.persist_block(block, user_id=user.id, session_id=session.id)
    await store.persist_block(block, user_id=user.id, session_id=session.id)

    pending = user_message.metadata_json["thinking_pending"]
    assert pending["status"] == "completed"
    assert len(pending["blocks"]) == 1
    assert pending["blocks"][0]["content"] == "分析品牌"


@pytest.mark.asyncio
async def test_persist_block_writes_existing_turn_assistant_directly(
    db_session, user_factory
) -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"
    user, session, user_message, assistant_message = await _seed_turn(
        db_session,
        user_factory,
        turn_id=turn_id,
        assistant_metadata={"turn_id": turn_id},
    )

    await ThinkingMessageStore(db_session).persist_block(
        _block(turn_id=turn_id),
        user_id=user.id,
        session_id=session.id,
    )

    assert assistant_message is not None
    assert assistant_message.metadata_json["thinking"]["blocks"][0]["content"] == "分析品牌"
    assert "thinking_pending" not in user_message.metadata_json


@pytest.mark.asyncio
async def test_persist_block_allows_task_id_assistant_fallback(db_session, user_factory) -> None:
    task_id = str(uuid4())
    user, session, _, assistant_message = await _seed_turn(
        db_session,
        user_factory,
        assistant_metadata={"task_id": task_id},
    )

    await ThinkingMessageStore(db_session).persist_block(
        _block(task_id=task_id),
        user_id=user.id,
        session_id=session.id,
    )

    assert assistant_message is not None
    assert assistant_message.metadata_json["thinking"]["blocks"][0]["operation_id"] == "op-1"


@pytest.mark.asyncio
async def test_retry_block_does_not_attach_to_source_assistant_with_same_turn(
    db_session, user_factory
) -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"
    user, session, user_message, source_assistant = await _seed_turn(
        db_session,
        user_factory,
        turn_id=turn_id,
        assistant_metadata={
            "turn_id": turn_id,
            "task_id": "source-task",
            "kind": "conclusion",
        },
    )
    store = ThinkingMessageStore(db_session)

    await store.persist_block(
        _block(
            turn_id=turn_id,
            operation_id="retry-op",
            task_id="retry-task",
        ),
        user_id=user.id,
        session_id=session.id,
    )

    assert source_assistant is not None
    assert "thinking" not in source_assistant.metadata_json
    assert user_message.metadata_json["thinking_pending"]["blocks"][0]["task_id"] == "retry-task"

    retry_assistant = Message(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        role="assistant",
        content="重试结论",
        sequence=3,
        metadata_json={"task_id": "retry-task", "kind": "conclusion"},
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(retry_assistant)
    await db_session.flush()
    await store.attach_turn_to_assistant(
        retry_assistant,
        user_id=user.id,
        session_id=session.id,
        turn_id=turn_id,
    )

    assert retry_assistant.metadata_json["thinking"]["blocks"][0]["operation_id"] == "retry-op"
    assert "thinking_pending" not in user_message.metadata_json


@pytest.mark.asyncio
async def test_thinking_flush_failure_keeps_business_session_usable(
    db_session, user_factory, monkeypatch
) -> None:
    user, session, user_message, _ = await _seed_turn(db_session, user_factory)
    injected = False
    begin_nested_calls = 0
    original_begin_nested = db_session.begin_nested

    def tracked_begin_nested():
        nonlocal begin_nested_calls
        begin_nested_calls += 1
        return original_begin_nested()

    monkeypatch.setattr(db_session, "begin_nested", tracked_begin_nested)

    def inject_duplicate_on_thinking_flush(sync_session, *_args) -> None:
        nonlocal injected
        if injected or "thinking_pending" not in (user_message.metadata_json or {}):
            return
        injected = True
        sync_session.add(
            Message(
                id=str(uuid4()),
                session_id=session.id,
                user_id=user.id,
                role="assistant",
                content="制造唯一键冲突",
                sequence=user_message.sequence,
                metadata_json={},
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

    event.listen(db_session.sync_session, "before_flush", inject_duplicate_on_thinking_flush)
    try:
        with pytest.raises(IntegrityError):
            await ThinkingMessageStore(db_session).persist_block(
                _block(),
                user_id=user.id,
                session_id=session.id,
            )
    finally:
        event.remove(
            db_session.sync_session,
            "before_flush",
            inject_duplicate_on_thinking_flush,
        )

    session.title = "业务事务仍可提交"
    await db_session.flush()
    assert begin_nested_calls == 1
    assert session.title == "业务事务仍可提交"


@pytest.mark.asyncio
async def test_attach_turn_moves_pending_blocks_to_assistant(db_session, user_factory) -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"
    user, session, user_message, assistant_message = await _seed_turn(
        db_session,
        user_factory,
        turn_id=turn_id,
        assistant_metadata={},
    )
    store = ThinkingMessageStore(db_session)
    await store.persist_block(_block(turn_id=turn_id), user_id=user.id, session_id=session.id)
    assert assistant_message is not None

    await store.attach_turn_to_assistant(
        assistant_message,
        user_id=user.id,
        session_id=session.id,
        turn_id=turn_id,
    )

    assert assistant_message.metadata_json["turn_id"] == turn_id
    assert assistant_message.metadata_json["thinking"]["blocks"][0]["content"] == "分析品牌"
    assert "thinking_pending" not in user_message.metadata_json


@pytest.mark.asyncio
async def test_interrupted_block_sets_top_level_status(db_session, user_factory) -> None:
    user, session, user_message, _ = await _seed_turn(db_session, user_factory)
    store = ThinkingMessageStore(db_session)

    await store.persist_block(
        _block(operation_id="op-ok"),
        user_id=user.id,
        session_id=session.id,
    )
    await store.persist_block(
        _block(operation_id="op-failed", status="interrupted"),
        user_id=user.id,
        session_id=session.id,
    )

    assert user_message.metadata_json["thinking_pending"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_record_brainstorm_failure_persists_turn_and_interrupted_thinking(
    db_session, user_factory
) -> None:
    user, session, _, _ = await _seed_turn(
        db_session,
        user_factory,
        assistant_metadata=None,
    )
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"

    class SameSessionFactory:
        @asynccontextmanager
        async def begin(self):
            yield db_session

    assistant = await record_brainstorm_failure(
        SameSessionFactory(),
        user_id=user.id,
        session_id=session.id,
        turn_id=turn_id,
        user_content="分析品牌",
        blocks=(_block(turn_id=turn_id, status="interrupted"),),
        error_code="model_unavailable",
    )
    messages = list(
        (
            await db_session.scalars(
                select(Message)
                .where(Message.user_id == user.id, Message.session_id == session.id)
                .order_by(Message.sequence)
            )
        ).all()
    )

    assert assistant.content == "模型暂时无法完成需求理解，请稍后重试。"
    assert assistant.metadata_json["turn_id"] == turn_id
    assert assistant.metadata_json["error_code"] == "model_unavailable"
    assert assistant.metadata_json["thinking"]["status"] == "interrupted"
    assert messages[-1].id == assistant.id
    assert "thinking_pending" not in messages[0].metadata_json


def _deadlock_error() -> OperationalError:
    orig = Exception()
    orig.args = (1213, "Deadlock found when trying to get lock; try restarting transaction")
    return OperationalError("", "", orig)


class _FakeStore:
    """记录调用并按实例序号（= 第几轮事务）注入死锁的 ThinkingMessageStore 替身。"""

    instances: list["_FakeStore"] = []
    fail_attempts: int = 0

    @classmethod
    def reset(cls, *, fail_attempts: int = 0) -> None:
        cls.instances = []
        cls.fail_attempts = fail_attempts

    def __init__(self, db) -> None:
        self.db = db
        self.persist_calls: list[ThinkingBlock] = []
        self.attach_calls: list[str] = []
        type(self).instances.append(self)

    async def persist_block(self, block, *, user_id, session_id) -> None:
        self.persist_calls.append(block)
        if len(type(self).instances) <= type(self).fail_attempts:
            raise _deadlock_error()

    async def attach_turn_to_assistant(self, message, *, user_id, session_id, turn_id) -> None:
        self.attach_calls.append(turn_id)


class _FakeThinkingService:
    def __init__(self, blocks: tuple[ThinkingBlock, ...]) -> None:
        self._blocks = blocks
        self.completed_calls = 0
        self.mark_calls: list[list[tuple[str, int]]] = []

    async def completed_blocks(self, *, turn_id, user_id, session_id, only_unpersisted=False):
        self.completed_calls += 1
        return self._blocks

    async def mark_blocks_persisted(self, *, turn_id, user_id, session_id, keys) -> None:
        self.mark_calls.append(list(keys))


class _SameSessionFactory:
    def __init__(self, db_session) -> None:
        self._db_session = db_session

    @asynccontextmanager
    async def begin(self):
        yield self._db_session


@pytest.mark.asyncio
async def test_persist_turn_thinking_marks_only_after_commit(
    db_session, user_factory, monkeypatch
) -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"
    user, session, _, assistant_message = await _seed_turn(
        db_session,
        user_factory,
        turn_id=turn_id,
        assistant_metadata={"turn_id": turn_id},
    )
    assert assistant_message is not None
    _FakeStore.reset(fail_attempts=1)
    monkeypatch.setattr("app.thinking.persistence.ThinkingMessageStore", _FakeStore)
    service = _FakeThinkingService((_block(turn_id=turn_id),))

    await persist_turn_thinking(
        _SameSessionFactory(db_session),
        service,
        user_id=user.id,
        session_id=session.id,
        turn_id=turn_id,
        assistant_message_id=assistant_message.id,
    )

    # 重试复用同一 blocks 列表：两轮事务各调用一次 persist_block。
    assert len(_FakeStore.instances) == 2
    assert [len(instance.persist_calls) for instance in _FakeStore.instances] == [1, 1]
    assert _FakeStore.instances[1].attach_calls == [turn_id]
    assert service.mark_calls == [[("op-1", 1)]]


@pytest.mark.asyncio
async def test_persist_turn_thinking_swallows_repeat_failure(
    db_session, user_factory, monkeypatch
) -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"
    user, session, _, assistant_message = await _seed_turn(
        db_session,
        user_factory,
        turn_id=turn_id,
        assistant_metadata={"turn_id": turn_id},
    )
    assert assistant_message is not None
    _FakeStore.reset(fail_attempts=2)
    monkeypatch.setattr("app.thinking.persistence.ThinkingMessageStore", _FakeStore)
    service = _FakeThinkingService((_block(turn_id=turn_id),))

    await persist_turn_thinking(
        _SameSessionFactory(db_session),
        service,
        user_id=user.id,
        session_id=session.id,
        turn_id=turn_id,
        assistant_message_id=assistant_message.id,
    )

    assert len(_FakeStore.instances) == 2
    assert service.mark_calls == []


@pytest.mark.asyncio
async def test_persist_turn_thinking_without_assistant_skips_attach(
    db_session, user_factory, monkeypatch
) -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"
    user, session, _, _ = await _seed_turn(db_session, user_factory, turn_id=turn_id)
    _FakeStore.reset()
    monkeypatch.setattr("app.thinking.persistence.ThinkingMessageStore", _FakeStore)
    service = _FakeThinkingService((_block(turn_id=turn_id),))

    await persist_turn_thinking(
        _SameSessionFactory(db_session),
        service,
        user_id=user.id,
        session_id=session.id,
        turn_id=turn_id,
    )

    assert len(_FakeStore.instances) == 1
    assert len(_FakeStore.instances[0].persist_calls) == 1
    assert _FakeStore.instances[0].attach_calls == []
    assert service.mark_calls == [[("op-1", 1)]]
