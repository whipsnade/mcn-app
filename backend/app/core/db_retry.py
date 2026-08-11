"""InnoDB 锁竞争的有界重试（死锁 1213 / 锁等待超时 1205）。

REPEATABLE READ 下并发写路径（Run/Attempt/Wallet/Session 行与间隙锁）可能
瞬时互锁；回滚后以全新事务按有界退避重试是标准处置。所有接入方必须是
幂等写路径（终态/账务端点均满足），非锁错误原样抛出。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")

_LOCK_RETRYABLE_ERRNOS = {1205, 1213}
_LOCK_RETRY_ATTEMPTS = 3


def is_retryable_lock_error(exc: BaseException) -> bool:
    origin = getattr(exc, "orig", exc)
    args = getattr(origin, "args", ())
    return bool(args) and args[0] in _LOCK_RETRYABLE_ERRNOS


async def with_lock_retry(
    db: AsyncSession,
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = _LOCK_RETRY_ATTEMPTS,
) -> T:
    for attempt in range(attempts):
        try:
            return await operation()
        except OperationalError as exc:
            if attempt + 1 >= attempts or not is_retryable_lock_error(exc):
                raise
            await db.rollback()
            await asyncio.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")
