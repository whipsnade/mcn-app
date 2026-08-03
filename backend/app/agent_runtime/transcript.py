"""Run 对话 transcript 重建（v3 加固 §5.4 / A4）。

恢复接管后模型必须看到本 Run 已完成的工具调用与结果，否则必然重复调用已
settled 的 MCP 工具 → 重复扣费。``RunTranscriptLoader`` 从触发消息 + 本 Run
完整 Step 重建模型上下文：

- 每个已完成（completed/failed）tool_call Step 回放其持久结果——settled 回放
  ``evidence_id + 结构化预览``（Step 的 ``output_json`` 即当初回喂模型的
  ``ToolResult``，只含 safe_summary/evidence_id，**绝不回灌 raw payload**，
  控制上下文预算）；failed/unknown 回放原结构化错误结果；
- 崩溃残留的 running tool_call Step（外发后 / settle 前崩溃）按
  ``agent_tool_calls`` 行当前状态构造结果回放（settled → success，
  其余 → unknown 待核对），并作为 ``resume_step`` 交给引擎：模型重新发起
  相同调用时复用该 Step（同一 ``logical_call_id``，协调器幂等回放，绝不
  重发、不重复扣费）——防重不依赖模型记忆；
- 恢复从最后一个完整 Step 的下一 sequence 继续（引擎 ``_next_step_sequence``
  取全部 Step 最大 sequence + 1，语义不变）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.schemas import CallTool
from app.agent_runtime.tools.mcp import FAILED_CONFIRMED, RESULT_UNKNOWN
from app.model.contracts import ChatMessage


@dataclass(frozen=True)
class RunTranscript:
    """一个 Run 的重建上下文：初始 messages + 待复用的崩溃残留 Step。"""

    messages: list[ChatMessage]
    resume_step: AgentStep | None


class RunTranscriptLoader:
    """从触发消息和完整 Step 重建本 Run 上下文（§5.4）。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def load(self, run: AgentRun) -> RunTranscript:
        """重建本 Run 的对话上下文（只读，不修改任何持久状态）。"""
        messages = [await self._trigger_message(run)]
        steps = (
            await self._db.scalars(
                select(AgentStep)
                .where(
                    AgentStep.run_id == run.id,
                    AgentStep.step_type == "tool_call",
                )
                .order_by(AgentStep.sequence)
            )
        ).all()
        resume_step: AgentStep | None = None
        for step in steps:
            if step.status == "running":
                # 崩溃残留：按调用行当前状态回放，并交给引擎复用（正常最多
                # 一个在飞调用；防御性地取最后一个 running Step）。
                result = await self._replay_from_call_row(step)
                resume_step = step
            else:
                result = dict(step.output_json or {})
            messages.append(self._action_message(step))
            messages.append(self._result_message(result))
        return RunTranscript(messages=messages, resume_step=resume_step)

    # ------------------------------------------------------------------ #
    # 触发消息
    # ------------------------------------------------------------------ #

    async def _trigger_message(self, run: AgentRun) -> ChatMessage:
        """优先取 Run 关联的输入消息，回退到会话最近一条用户消息。"""
        if run.input_message_id is not None:
            message = await self._db.get(AgentMessage, run.input_message_id)
            if message is not None:
                return ChatMessage(role=message.role, content=message.content)
        latest = await self._db.scalar(
            select(AgentMessage)
            .where(
                AgentMessage.session_id == run.session_id,
                AgentMessage.role == "user",
            )
            .order_by(AgentMessage.sequence.desc())
            .limit(1)
        )
        if latest is not None:
            return ChatMessage(role="user", content=latest.content)
        return ChatMessage(role="user", content="")

    # ------------------------------------------------------------------ #
    # 崩溃残留 Step 的回放结果
    # ------------------------------------------------------------------ #

    async def _replay_from_call_row(self, step: AgentStep) -> dict[str, Any]:
        """按 ``agent_tool_calls`` 行当前状态构造回放结果（与协调器 _replay 同语义）。

        - settled：取回 Evidence，回放 ``evidence_id + 结构化预览``（与正常工具
          返回形态一致，不回灌 raw payload）；
        - failed：回放原结构化错误；
        - 其他（unknown/running/reserved/无调用行）：回放 unknown 待核对——
          绝不自动重放（§11.1）。
        """
        call = await self._db.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.step_id == step.id)
            .order_by(AgentToolCall.started_at.desc())
            .limit(1)
        )
        if call is None:
            return {
                "status": "unknown",
                "safe_summary": "tool call interrupted before dispatch",
                "error_type": RESULT_UNKNOWN,
            }
        if call.status == "settled":
            evidence = await EvidenceWriter(self._db).get_by_tool_call_id(call.id)
            if evidence is not None:
                return {
                    "status": "success",
                    "safe_summary": json.dumps(
                        evidence.normalized_preview_json, ensure_ascii=False
                    )[:1_000],
                    "evidence_id": evidence.id,
                }
            return {
                "status": "success",
                "safe_summary": "confirmed success (payload unavailable)",
            }
        if call.status == "failed":
            return {
                "status": "failed",
                "safe_summary": call.safe_error_message or "tool call failed",
                "error_type": call.error_type or FAILED_CONFIRMED,
            }
        return {
            "status": "unknown",
            "safe_summary": call.safe_error_message
            or "tool call interrupted; result pending reconciliation",
            "error_type": RESULT_UNKNOWN,
        }

    # ------------------------------------------------------------------ #
    # 回放消息形态（与引擎正常循环完全一致）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _action_message(step: AgentStep) -> ChatMessage:
        """重建当初模型输出的 call_tool 动作消息（同 ``AgentEngine._action_message``）。"""
        input_json = step.input_json or {}
        action = CallTool(
            action="call_tool",
            internal_tool_name=input_json.get("internal_tool_name", "unknown"),
            arguments=input_json.get("arguments") or {},
            rationale="（恢复回放）本 Run 此前已发起的工具调用",
        )
        return ChatMessage(role="assistant", content=action.model_dump_json())

    @staticmethod
    def _result_message(result: dict[str, Any]) -> ChatMessage:
        """构造 tool_result 用户消息（同 ``AgentEngine._feed_tool_result``）。"""
        return ChatMessage(
            role="user",
            content=json.dumps(
                {
                    "tool_result": {
                        "status": result.get("status", "unknown"),
                        "summary": result.get("safe_summary"),
                        "evidence_id": result.get("evidence_id"),
                        "cursor": result.get("cursor"),
                        "truncated": bool(result.get("truncated", False)),
                        "error_type": result.get("error_type"),
                    }
                },
                ensure_ascii=False,
            ),
        )


__all__ = ["RunTranscript", "RunTranscriptLoader"]
