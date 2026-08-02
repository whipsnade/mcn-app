"""统一模型决策网关（设计文档 §10.5 / §六 / Task 6）。

每个 Agent Profile 的模型决策都经过 ``AgentModelGateway``：一次调用、一个
``AgentStep`` 审计（running → completed/failed）。职责：

- 思考流：只转发供应商实际暴露的 ``reasoning_content`` / ``<think>``，通过
  ``_GatedThinkingSink`` 延迟补发 ``started``，供应商无思考时后端不产生任何
  ``thinking.*`` 事件（spec §10.5）；
- 动作解析：把四种动作协议（Task 5 的 ``AgentAction``）作为输出 Schema 交给
  适配器严格校验与修复，最终经 ``AGENT_ACTION_ADAPTER`` 解析成冻结动作；
- Step 审计：调用前落一条 running Step，结束/失败后补齐输出、用量、请求 ID
  与脱敏 thinking（≤ 64 KiB），内部 Run 的 Step 标记 internal。

不重写任何供应商调用：完整复用 ``TencentPlanAdapter``，本网关只做组合。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import RootModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun, AgentStep
from app.agent_runtime.profiles import AgentProfile
from app.agent_runtime.prompts import get_system_prompt
from app.agent_runtime.schemas import AGENT_ACTION_ADAPTER, AgentAction
from app.core.redaction import redact_for_log
from app.model.contracts import (
    ChatMessage,
    ModelPurpose,
    StructuredModelRequest,
    ThinkingSink,
)
from app.model.tencent_plan import TencentPlanAdapter


logger = logging.getLogger(__name__)

# agent_steps.thinking_text 单次上限（spec §10.5）：64 KiB。
MAX_THINKING_TEXT_CHARS = 64 * 1024

# 四种动作协议（判别联合）作为模型输出 Schema。用 RootModel 包装使
# ``model_json_schema`` 产出 oneOf + discriminator 的完整动作 Schema，
# 且 ``model_validate_json`` 能严格校验——适配器的 validate_with_repair 与
# 修复重试循环因此能直接作用于动作协议本身。
class _AgentActionRoot(RootModel[AgentAction]):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class _GatedThinkingSink:
    """延迟转发 ``thinking.started``，直到第一次真实思考 delta 到达。

    适配器无论供应商是否返回思考都会先调用 ``sink.started``；本网关按
    attempt 对齐：某次 attempt 只有产生了实际思考 delta 才补发它的
    ``started``（及后续 delta/completed/failed）。供应商无思考时调用方
    收不到任何 ``thinking.*`` 事件。思考文本始终累积到 ``parts`` 供 Step
    审计脱敏写入，与是否转发无关。
    """

    def __init__(self, real: ThinkingSink | None) -> None:
        self._real = real
        self._forwarded_attempts: set[int] = set()
        self.parts: list[str] = []

    async def started(self, *, attempt: int) -> None:
        # 不立即转发；等该 attempt 的第一段真实 delta 到达再补发。
        return None

    async def delta(self, text: str, *, attempt: int) -> None:
        if text:
            self.parts.append(text)
        if self._real is None:
            return
        if attempt not in self._forwarded_attempts:
            self._forwarded_attempts.add(attempt)
            await self._safe_call(self._real.started, attempt=attempt)
        await self._safe_call(self._real.delta, text, attempt=attempt)

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        if self._real is not None and attempt in self._forwarded_attempts:
            await self._safe_call(
                self._real.completed, attempt=attempt, duration_ms=duration_ms
            )

    async def failed(self, *, attempt: int, error_code: str) -> None:
        if self._real is not None and attempt in self._forwarded_attempts:
            await self._safe_call(
                self._real.failed, attempt=attempt, error_code=error_code
            )

    @staticmethod
    async def _safe_call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            await method(*args, **kwargs)
        except Exception:
            logger.warning("thinking sink call failed", exc_info=True)


class AgentModelGateway:
    """每个 Profile 的模型决策统一入口：一次决策 = 一次模型调用 + 一条 Step 审计。"""

    def __init__(
        self,
        adapter: TencentPlanAdapter,
        *,
        db: AsyncSession,
        sanitize_thinking: Callable[[str], str] = redact_for_log,
    ) -> None:
        self._adapter = adapter
        self._db = db
        self._sanitize_thinking = sanitize_thinking

    async def decide(
        self,
        *,
        run: AgentRun,
        attempt_id: str,
        profile: AgentProfile,
        messages: list[ChatMessage],
        thinking_sink: ThinkingSink | None,
        step_sequence: int,
        purpose: ModelPurpose = "agent_loop",
        template_name: str = "agent_loop_v1",
    ) -> AgentAction:
        full_messages = self._prepend_system_prompt(profile, messages)
        # 总是带 gated sink：内部 Run 传 None 时仍走流式路径以捕获思考审计。
        gated = _GatedThinkingSink(thinking_sink)

        # 1) 调用前先落 running Step（input_json = 发送给模型的消息）。
        step = AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt_id,
            sequence=step_sequence,
            step_type="model_decision",
            input_json=[message.model_dump() for message in full_messages],
            status="running",
            thinking_text=None,
            visibility=run.visibility,
            created_at=_utc_now(),
        )
        self._db.add(step)
        await self._db.flush()

        # 2) 复用适配器流式调用：只转发供应商暴露的 thinking，JSON 修复在适配器内。
        started = time.monotonic()
        request = StructuredModelRequest(
            purpose=purpose,
            template_name=template_name,
            messages=tuple(full_messages),
            output_model=_AgentActionRoot,
            thinking_sink=gated,
        )
        try:
            result = await self._adapter.complete_json(request)
        except BaseException:
            # 失败/取消也必须给出 Step 终态，避免运行中快照残留。
            await self._mark_step_failed(step, started, gated)
            raise

        # 3) 冻结动作协议解析（Task 5）。
        action = AGENT_ACTION_ADAPTER.validate_python(result.value.root)

        # 4) 补齐 Step：输出、用量、请求 ID、脱敏 thinking。
        step.status = "completed"
        step.duration_ms = _elapsed_ms(started)
        step.output_json = action.model_dump()
        step.token_usage_json = (
            result.usage.model_dump() if result.usage is not None else None
        )
        step.model_request_id = result.request_id
        step.thinking_text = self._finalize_thinking(gated)
        await self._db.flush()
        return action

    def _prepend_system_prompt(
        self, profile: AgentProfile, messages: list[ChatMessage]
    ) -> list[ChatMessage]:
        if messages and messages[0].role == "system":
            return list(messages)
        system_text = get_system_prompt(profile.system_prompt_key).text
        return [ChatMessage(role="system", content=system_text), *messages]

    def _finalize_thinking(self, gated: _GatedThinkingSink) -> str | None:
        if not gated.parts:
            return None
        joined = "".join(gated.parts)
        # spec §10.5：与 Prompt 日志相同的密钥/token 脱敏，再截断到 64 KiB。
        return self._sanitize_thinking(joined)[:MAX_THINKING_TEXT_CHARS]

    async def _mark_step_failed(
        self, step: AgentStep, started: float, gated: _GatedThinkingSink
    ) -> None:
        step.status = "failed"
        step.duration_ms = _elapsed_ms(started)
        try:
            step.thinking_text = self._finalize_thinking(gated)
            await self._db.flush()
        except Exception:
            logger.exception("failed to finalize failed agent step")


__all__ = ["AgentModelGateway", "MAX_THINKING_TEXT_CHARS"]
