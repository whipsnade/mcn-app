"""AgentEventThinkingSink（v3 加固 §5.8 / §10.5 / §13.1）。

把模型网关转发的真实 thinking 回调持久化为 ``thinking.started/delta/
completed/failed`` 事件（``AgentEventStream``，per-run sequence），供前端
SSE 实时展开/折叠展示。

约束（与 model_gateway 的 Step 审计一致）：

- **只注入用户可见 Run**：session_analyst 主 Run 与 kol_detail Run 由执行层
  （executor / KolDetailRunService）经 ``AgentEngine.thinking_sink_for`` 注入；
  Reviewer/Utility 内部 Run 不注入（只写 internal Step 审计，不发事件）；
- **只有真实 thinking 才发事件**：与 ``_GatedThinkingSink`` 配合——供应商无
  reasoning_content/``<think>`` 时网关根本不会回调本 sink，零 thinking 事件；
- **脱敏 + 64 KiB 上限**：delta 文本逐条 ``redact_for_log`` 脱敏，累计超过
  ``MAX_THINKING_TEXT_CHARS``（64 KiB）后丢弃后续 delta（completed/failed
  终态事件仍发出，前端可折叠收尾）。

sink 异常不阻塞主流程：网关侧 ``_GatedThinkingSink._safe_call`` 已兜底
（记 warning 后继续），因此本类不做静默吞错，让异常可被观察。
"""

from __future__ import annotations

from collections.abc import Callable

from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.model_gateway import MAX_THINKING_TEXT_CHARS
from app.core.redaction import redact_for_log


class AgentEventThinkingSink:
    """把真实 thinking delta 转为 thinking.* 持久事件（ThinkingSink 协议实现）。"""

    def __init__(
        self,
        events: AgentEventStream,
        *,
        run_id: str,
        user_id: str,
        sanitize: Callable[[str], str] = redact_for_log,
        max_chars: int = MAX_THINKING_TEXT_CHARS,
    ) -> None:
        self._events = events
        self._run_id = run_id
        self._user_id = user_id
        self._sanitize = sanitize
        self._max_chars = max_chars
        self._emitted_chars = 0

    async def started(self, *, attempt: int) -> None:
        await self._events.append(
            self._run_id, self._user_id, "thinking.started", {"attempt": attempt}
        )

    async def delta(self, text: str, *, attempt: int) -> None:
        if not text or self._emitted_chars >= self._max_chars:
            return
        remaining = self._max_chars - self._emitted_chars
        safe = self._sanitize(text)[:remaining]
        if not safe:
            return
        self._emitted_chars += len(safe)
        await self._events.append(
            self._run_id,
            self._user_id,
            "thinking.delta",
            {"attempt": attempt, "text": safe},
        )

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        await self._events.append(
            self._run_id,
            self._user_id,
            "thinking.completed",
            {"attempt": attempt, "duration_ms": duration_ms},
        )

    async def failed(self, *, attempt: int, error_code: str) -> None:
        await self._events.append(
            self._run_id,
            self._user_id,
            "thinking.failed",
            {"attempt": attempt, "error_code": error_code},
        )


__all__ = ["AgentEventThinkingSink"]
