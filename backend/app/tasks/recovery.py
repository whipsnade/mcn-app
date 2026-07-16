from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol


class RecoveryStore(Protocol):
    async def recoverable_task_ids(self) -> tuple[str, ...]: ...

    async def release_expired_unknown(self, task_id: str, observation_seconds: int) -> bool: ...

    async def pending_followup_task_ids(self) -> tuple[str, ...]: ...


class RecoveryRunner(Protocol):
    def submit(self, task_id: str) -> None: ...


class TaskRecovery:
    """恢复过期租约；unknown 只协调释放，绝不重发远端 MCP 调用。"""

    def __init__(
        self,
        *,
        repository: RecoveryStore,
        runner: RecoveryRunner,
        observation_seconds: int = 300,
        followup_generator: Callable[[str], Awaitable[bool]] | None = None,
        followup_preparer: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self.repository = repository
        self.runner = runner
        self.observation_seconds = observation_seconds
        self.followup_generator = followup_generator
        self.followup_preparer = followup_preparer

    async def recover_expired(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for task_id in await self.repository.recoverable_task_ids():
            if await self.repository.release_expired_unknown(task_id, self.observation_seconds):
                continue
            self.runner.submit(task_id)
            recovered.append(task_id)
        return tuple(recovered)

    async def recover_pending_followups(self) -> tuple[str, ...]:
        if self.followup_generator is None:
            return ()
        generated: list[str] = []
        for task_id in await self.repository.pending_followup_task_ids():
            try:
                if self.followup_preparer is not None and not await self.followup_preparer(task_id):
                    continue
                if await self.followup_generator(task_id):
                    generated.append(task_id)
            except Exception:
                # Follow-up generation is non-fatal; the next recovery pass may retry it.
                continue
        return tuple(generated)
