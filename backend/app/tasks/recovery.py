from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.tasks.executor import TaskExecutor


class RecoveryStore(Protocol):
    async def recoverable_task_ids(self) -> tuple[str, ...]: ...

    async def release_expired_unknown(self, task_id: str, observation_seconds: int) -> bool: ...


class TaskRecovery:
    """恢复过期租约；unknown 只协调释放，绝不重发远端 MCP 调用。"""

    def __init__(
        self,
        *,
        repository: RecoveryStore,
        executor_factory: Callable[[], TaskExecutor],
        observation_seconds: int = 300,
    ) -> None:
        self.repository = repository
        self.executor_factory = executor_factory
        self.observation_seconds = observation_seconds

    async def recover_expired(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for task_id in await self.repository.recoverable_task_ids():
            if await self.repository.release_expired_unknown(task_id, self.observation_seconds):
                continue
            await self.executor_factory().run(task_id)
            recovered.append(task_id)
        return tuple(recovered)
