"""Run 对话 transcript 重建（v3 加固 §5.4 / A4 / Gate A G3）。

恢复接管后模型必须看到本 Run 已完成的工具调用与结果，否则必然重复调用已
settled 的 MCP 工具 → 重复扣费。``RunTranscriptLoader`` 从触发消息 + 本 Run
完整 Step 重建模型上下文：

- 每个已完成（completed/failed）tool_call Step 回放其持久结果——settled 回放
  ``evidence_id + 结构化预览``（Step 的 ``output_json`` 即当初回喂模型的
  ``ToolResult``，只含 safe_summary/evidence_id，**绝不回灌 raw payload**，
  控制上下文预算）；failed/unknown 回放原结构化错误结果；
- 崩溃残留的 running Step 在恢复装载时先做持久化收口：已确认 settled 的
  tool_call 标为 completed，其余 tool_call / model_decision 标为 failed；
  tool_call 仍按 ``agent_tool_calls`` 行当前状态构造结果回放（settled →
  success，其余 → unknown 待核对），并作为 ``resume_step`` 交给引擎：模型
  重新发起相同调用时复用该 Step（同一 ``logical_call_id``，协调器幂等回放，
  绝不重发、不重复扣费）。这样统一完成门禁看到的永远不是遗留的 running
  Step；
- 恢复从最后一个完整 Step 的下一 sequence 继续（引擎 ``_next_step_sequence``
  取全部 Step 最大 sequence + 1，语义不变）；
- **显式用户问题锚点（G3）**：tool_result 回放同样是 ``role="user"`` 消息，
  引擎若从消息列表尾部反推「当前用户问题」会把结构化工具结果误当用户意图
  （Memory Header / Reviewer 上下文被污染）。``RunTranscript.user_question``
  显式携带触发消息内容，引擎必须优先使用它；kol_detail 等无
  ``input_message_id`` 的 Run 从 ``prompt_snapshot_json`` 的触发上下文
  （platform/kol_uid）恢复，绝不回退到会话最近一条普通用户消息。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.evidence import EvidenceWriter, build_model_evidence_view
from app.agent_runtime.kol_detail import (
    KOL_DETAIL_SNAPSHOT_KEY,
    kol_detail_trigger_content,
)
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
    """一个 Run 的重建上下文：初始 messages + 待复用的崩溃残留 Step + 锚点。"""

    messages: list[ChatMessage]
    resume_step: AgentStep | None
    # 显式用户问题锚点（G3）：触发消息内容（role="user" 时，否则空串）。
    # 引擎优先使用它，不再从消息列表尾部反推（尾部可能是 tool_result 回放）。
    user_question: str


class RunTranscriptLoader:
    """从触发消息和完整 Step 重建本 Run 上下文（§5.4）。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def load(self, run: AgentRun) -> RunTranscript:
        """重建上下文并收口崩溃残留 Step。

        这里只处理本 Run 已经存在的崩溃审计行：不会重发工具，也不会把
        unknown ToolCall 改成成功；它只是把无法继续保持 ``running`` 的本地
        Step 标为 completed/failed，随后由 CompletionValidator 继续检查
        ToolCall 与 permit 是否仍未决。
        """
        trigger = await self._trigger_message(run)
        messages = [trigger]
        steps = (
            await self._db.scalars(
                select(AgentStep)
                .where(AgentStep.run_id == run.id)
                .order_by(AgentStep.sequence)
            )
        ).all()
        resume_step: AgentStep | None = None
        reconciled = False
        for step in steps:
            if step.status == "running":
                if step.step_type == "tool_call":
                    # 崩溃残留：按调用行当前状态回放，并交给引擎复用（正常最多
                    # 一个在飞调用；防御性地取最后一个 running Step）。
                    result = await self._replay_from_call_row(step)
                    step.status = (
                        "completed" if result.get("status") == "success" else "failed"
                    )
                    step.output_json = result
                    reconciled = True
                    resume_step = step
                    messages.append(self._action_message(step))
                    messages.append(self._result_message(result))
                else:
                    # 模型决策本身不能重放；标为恢复中断后从触发消息继续。
                    step.status = "failed"
                    step.output_json = {
                        "status": "failed",
                        "safe_summary": "model decision interrupted before durable output",
                        "error_type": "recovery_interrupted",
                    }
                    reconciled = True
            elif step.step_type == "tool_call":
                result = dict(step.output_json or {})
                messages.append(self._action_message(step))
                messages.append(self._result_message(result))
        if reconciled:
            await self._db.flush()
        return RunTranscript(
            messages=messages,
            resume_step=resume_step,
            user_question=trigger.content if trigger.role == "user" else "",
        )

    # ------------------------------------------------------------------ #
    # 触发消息
    # ------------------------------------------------------------------ #

    async def _trigger_message(self, run: AgentRun) -> ChatMessage:
        """优先级：Run 关联输入消息 → ``prompt_snapshot_json`` 触发上下文
        （kol_detail 等无输入消息 Run 的 G3 恢复锚点）→ 会话最近一条用户消息。"""
        if run.input_message_id is not None:
            message = await self._db.get(AgentMessage, run.input_message_id)
            if message is not None:
                return ChatMessage(role=message.role, content=message.content)
        snapshot_trigger = self._snapshot_trigger_message(run)
        if snapshot_trigger is not None:
            return snapshot_trigger
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

    @staticmethod
    def _snapshot_trigger_message(run: AgentRun) -> ChatMessage | None:
        """从 ``prompt_snapshot_json`` 恢复 kol_detail 触发上下文（platform/kol_uid
        及经归属校验的名单引用，§6.4）。

        kol_detail Run 由点击触发、没有 ``input_message_id``：触发上下文在
        创建时持久化到 ``prompt_snapshot_json``（G3），崩溃接管按它恢复，
        绝不回退到会话最近一条普通用户消息（可能是完全无关的意图）。
        """
        snapshot = run.prompt_snapshot_json
        if not isinstance(snapshot, dict):
            return None
        trigger = snapshot.get(KOL_DETAIL_SNAPSHOT_KEY)
        if not isinstance(trigger, dict):
            return None
        platform = trigger.get("platform")
        kol_uid = trigger.get("kol_uid")
        if not platform or not kol_uid:
            return None
        return ChatMessage(
            role="user",
            content=kol_detail_trigger_content(
                str(platform),
                str(kol_uid),
                selection_artifact_id=trigger.get("selection_artifact_id"),
                selection_version=trigger.get("selection_version"),
            ),
        )

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
                        build_model_evidence_view(evidence), ensure_ascii=False
                    ),
                    "evidence_id": evidence.id,
                }
            if call.error_type in ("succeeded_empty", "result_unavailable"):
                return {
                    "status": "failed",
                    "safe_summary": call.safe_error_message
                    or ("confirmed MCP success without a retrievable payload"
                        if call.error_type == "result_unavailable"
                        else "upstream returned no structured content"),
                    "error_type": call.error_type,
                }
            return {
                "status": "unknown",
                "safe_summary": "settled tool call is missing Evidence",
                "error_type": RESULT_UNKNOWN,
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
