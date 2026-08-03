"""统一模型决策网关（设计文档 §10.5 / §六 / Task 6；v3 加固 §5.8）。

每个 Agent Profile 的模型决策都经过 ``AgentModelGateway``：一次调用、一个
``AgentStep`` 审计（running → completed/failed）。职责：

- 思考流：只转发供应商实际暴露的 ``reasoning_content`` / ``<think>``，通过
  ``_GatedThinkingSink`` 延迟补发 ``started``，供应商无思考时后端不产生任何
  ``thinking.*`` 事件（spec §10.5）；
- 动作解析：把动作/输出 Schema（默认是 Task 5 的 ``AgentAction`` 判别联合）
  作为输出 Schema 交给适配器严格校验与修复，返回适配器已校验的 payload，
  网关不再重复解析；自定义输出 Schema 的 Profile（Reviewer/Utility）通过
  ``decision_root`` / ``decision_adapter`` 复用本入口；
- 非法输出分层（§5.8）：适配器单次修复后仍非法的 JSON 输出是可恢复结果
  ``InvalidModelOutput``，交给 Engine 计入无效动作并回喂；供应商错误、
  鉴权错误与不可恢复协议错误仍按系统错误抛出。只有默认动作协议路径
  （``decision_root is _AgentActionRoot``）做该转换——Reviewer/Utility 是
  一次性调用，其驱动方已按系统失败收口，维持抛出语义；
- Step 审计：调用前落一条 running Step，结束/失败后补齐输出、用量、请求 ID
  与脱敏 thinking（≤ 64 KiB），内部 Run 的 Step 标记 internal。

不重写任何供应商调用：完整复用 ``TencentPlanAdapter``，本网关只做组合。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, RootModel, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun, AgentStep
from app.agent_runtime.profiles import AgentProfile
from app.agent_runtime.prompts import get_system_prompt
from app.agent_runtime.schemas import AgentAction
from app.core.redaction import redact_for_log
from app.model.contracts import (
    ChatMessage,
    ModelPlanInvalidError,
    ModelPurpose,
    StructuredModelRequest,
    ThinkingSink,
)
from app.model.tencent_plan import TencentPlanAdapter


logger = logging.getLogger(__name__)

# agent_steps.thinking_text 单次上限（spec §10.5）：64 KiB。
MAX_THINKING_TEXT_CHARS = 64 * 1024


@dataclass(frozen=True)
class InvalidModelOutput:
    """适配器单次修复后仍非法的模型输出（可恢复结果，§5.8）。

    只用于默认四种动作协议路径：Engine 收到后按无效动作计数并结构化回喂，
    连续达到统一上限才收口 failed；供应商/鉴权/不可恢复协议错误不走本类型，
    仍按系统错误抛出。
    """

    code: str = "MODEL_PLAN_INVALID"
    request_id: str | None = None

# 四种动作协议（判别联合）作为模型输出 Schema。用 RootModel 包装使
# ``model_json_schema`` 产出 oneOf + discriminator 的完整动作 Schema，
# 且 ``model_validate_json`` 能严格校验——适配器的 validate_with_repair 与
# 修复重试循环因此能直接作用于动作协议本身。
class _AgentActionRoot(RootModel[AgentAction]):
    pass


_DecisionT = TypeVar("_DecisionT")


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _output_json(decision: Any) -> Any:
    """Step 审计输出序列化：BaseModel 实例 dump，其他（dict/str 等）原样。"""
    if isinstance(decision, BaseModel):
        return decision.model_dump()
    return decision


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
    """每个 Profile 的模型决策统一入口：一次决策 = 一次模型调用 + 一条 Step 审计。

    ``decide`` 默认走 ``AgentAction`` 判别联合（``decision_root`` 默认
    ``_AgentActionRoot``，payload 即受控动作，无需 adapter 再转）。Reviewer
    （approve/revise/reject）与 Utility（utility JSON）等自定义输出 Schema
    的 Profile 传入各自的 ``decision_root``（RootModel 包装的 Schema 作为模型
    输出约束）与可选的 ``decision_adapter``（把 root payload 转成强类型决策）。
    """

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
        decision_root: type[RootModel[_DecisionT]] = _AgentActionRoot,
        decision_adapter: TypeAdapter[_DecisionT] | None = None,
    ) -> _DecisionT:
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

        # 2) 复用适配器流式调用：只转发供应商暴露的 thinking，JSON 修复与
        #    Schema 严格校验都在适配器内。log_context 供 prompt 学习日志归属。
        started = time.monotonic()
        request = StructuredModelRequest(
            purpose=purpose,
            template_name=template_name,
            messages=tuple(full_messages),
            output_model=decision_root,
            log_context={
                "user_id": run.user_id,
                "session_id": run.session_id,
                "task_id": run.id,
            },
            thinking_sink=gated,
        )
        try:
            result = await self._adapter.complete_json(request)
            # 3) 适配器已按 decision_root 严格校验过；root payload 即最终决策。
            #    默认 AgentAction 路径直接使用，不重复解析；自定义路径可再经
            #    decision_adapter 强类型化。
            decision = result.value.root
            if decision_adapter is not None:
                decision = decision_adapter.validate_python(decision)
        except ModelPlanInvalidError as exc:
            # §5.8 分层：修复后仍非法的 JSON 输出是可恢复结果，交 Engine 计入
            # 无效动作并回喂，不直接把 Run 失败。仅默认动作协议路径转换；
            # Reviewer/Utility 自定义路径是一次性调用，维持抛出（其驱动方按
            # 系统失败收口）。
            await self._mark_step_failed(step, started, gated)
            if decision_root is _AgentActionRoot:
                logger.info(
                    "model output invalid after repair for run %s; feeding back",
                    run.id,
                )
                return InvalidModelOutput(code=exc.code, request_id=exc.request_id)  # type: ignore[return-value]
            raise
        except (Exception, asyncio.CancelledError):
            # 失败/取消也必须给出 Step 终态，避免运行中快照残留。
            await self._mark_step_failed(step, started, gated)
            raise

        # 4) 补齐 Step：输出、用量、请求 ID、脱敏 thinking。
        step.status = "completed"
        step.duration_ms = _elapsed_ms(started)
        step.output_json = _output_json(decision)
        step.token_usage_json = (
            result.usage.model_dump() if result.usage is not None else None
        )
        step.model_request_id = result.request_id
        step.thinking_text = self._finalize_thinking(gated)
        await self._db.flush()
        return decision

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


__all__ = ["AgentModelGateway", "InvalidModelOutput", "MAX_THINKING_TEXT_CHARS"]
