"""InnoDB 死锁/锁等待重试助手的确定性测试。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.pi_gateway.router import _with_lock_retry


class _FakeMySQLError(Exception):
    pass


def _lock_error(errno: int) -> OperationalError:
    return OperationalError("stmt", {}, _FakeMySQLError(errno, "lock"))


class _FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_deadlock_retries_with_fresh_transaction() -> None:
    db = _FakeSession()
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _lock_error(1213)
        return "ok"

    assert await _with_lock_retry(db, operation) == "ok"  # type: ignore[arg-type]
    assert calls["n"] == 3
    assert db.rollbacks == 2


@pytest.mark.asyncio
async def test_lock_wait_timeout_is_retryable_and_bounded() -> None:
    db = _FakeSession()

    async def operation() -> str:
        raise _lock_error(1205)

    with pytest.raises(OperationalError):
        await _with_lock_retry(db, operation)  # type: ignore[arg-type]
    assert db.rollbacks == 2  # 3 次尝试，最后一次直接抛出


@pytest.mark.asyncio
async def test_non_lock_errors_propagate_without_retry() -> None:
    db = _FakeSession()
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        raise OperationalError("stmt", {}, _FakeMySQLError(1062, "duplicate"))

    with pytest.raises(OperationalError):
        await _with_lock_retry(db, operation)  # type: ignore[arg-type]
    assert calls["n"] == 1
    assert db.rollbacks == 0
