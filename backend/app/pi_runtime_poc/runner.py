"""方案 A 的单 Run Pi RPC 适配器。

Pi 只负责临时研究循环；Run/Attempt、租约、产品事件与终态始终由 FastAPI 持久化。
本模块不做意图路由、工具重试或积分处理。
"""

import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.events import AgentEventStream, map_pi_rpc_event
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    EvidenceItem,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import RunStatus
from app.core.config import Settings
from app.pi_runtime_poc.audit import PiRunAuditWriter
from app.pi_runtime_poc.auth import PiPocSettingsGuard, issue_run_token
from app.pi_runtime_poc.diagnostics import safe_db_diagnostic
from app.pi_runtime_poc.rpc import PiRpcProtocolError, _project_rpc_event


class PiRpcSession(Protocol):
    async def prompt(self, message: str) -> str: ...

    async def events(self) -> AsyncIterator[dict[str, Any]]: ...

    async def abort(self) -> None: ...

    async def close(self) -> None: ...


PiClientFactory = Callable[
    [AgentRun, str], PiRpcSession | Awaitable[PiRpcSession]
]
_THINKING_CHUNK_BYTES = 1024
_DIAGNOSTICS_LOGGER = logging.getLogger("pi_runtime_poc.diagnostics")


class PiPocRunner:
    """驱动一个 Pi Run 到唯一终态；基础设施失败不自动重启。"""

    def __init__(
        self,
        *,
        db: AsyncSession,
        events: AgentEventStream,
        settings: Settings,
        worker_id: str,
        client_factory: PiClientFactory,
    ) -> None:
        self._db = db
        self._events = events
        self._settings = settings
        self._worker_id = worker_id
        self._client_factory = client_factory
        self._thinking_parts: list[str] = []
        self._thinking_bytes = 0

    async def run(self, run_id: str) -> str | None:
        """领取、运行并用 ``settle_terminal`` 原子收口一个 queued Run。"""
        PiPocSettingsGuard.assert_safe(self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            return None
        user_id = run.user_id
        if run.cancel_requested:
            await self._settle(run_id, user_id, RunStatus.CANCELLED, {"reason": "cancel_requested"})
            return RunStatus.CANCELLED.value

        repo = AgentRunRepository(self._db)
        attempt = await repo.begin_attempt(run_id)
        if not await repo.claim_lease(
            run_id, self._worker_id, self._settings.pi_runtime_poc_run_timeout_seconds
        ):
            await self._db.rollback()
            return None
        await self._db.commit()
        attempt_id = attempt.id
        attempt_number = attempt.attempt
        await self._events.append(run_id, user_id, "run.started", {"attempt": attempt_number})

        token = issue_run_token(run_id, settings=self._settings)
        client: PiRpcSession | None = None
        assistant_parts: list[str] = []
        try:
            # ``append`` 是提交点，会使 ORM 对象过期；重新读取后才交给工厂。
            current_run = await self._db.get(AgentRun, run_id)
            if current_run is None:
                raise LookupError("run_not_found")
            created = self._client_factory(current_run, token)
            client = await created if inspect.isawaitable(created) else created
            await client.prompt(await self._build_start_message(run_id))
            async for raw_event in client.events():
                event = _project_rpc_event(raw_event)
                if await self._cancel_requested(run_id):
                    await client.abort()
                    await self._flush_thinking(run_id)
                    await self._settle(
                        run_id, user_id, RunStatus.CANCELLED, {"reason": "cancel_requested"}
                    )
                    return RunStatus.CANCELLED.value
                if await self._attempt_timed_out(attempt_id):
                    await client.abort()
                    await self._flush_thinking(run_id)
                    await self._settle(
                        run_id,
                        user_id,
                        RunStatus.FAILED,
                        {"error_code": "pi_run_timeout"},
                    )
                    return RunStatus.FAILED.value
                mapped = map_pi_rpc_event(event)
                if mapped is not None and mapped.event_type.value == "thinking.delta":
                    self._buffer_thinking(str(mapped.payload["text"]))
                    if self._thinking_bytes >= _THINKING_CHUNK_BYTES:
                        await self._flush_thinking(run_id)
                    continue
                await self._flush_thinking(run_id)
                await self._audit_event(run_id, event)
                if mapped is not None:
                    if mapped.event_type.value == "message.delta":
                        text = mapped.payload.get("text")
                        if isinstance(text, str):
                            assistant_parts.append(text)
                    if mapped.event_type.value == "thinking.failed":
                        await client.abort()
                        await self._flush_thinking(run_id)
                        await self._settle(
                            run_id,
                            user_id,
                            RunStatus.FAILED,
                            {"error_code": "pi_rpc_error"},
                        )
                        return RunStatus.FAILED.value
                if await self._clarification_requested(run_id):
                    # 澄清工具已通过既有状态机释放租约并结束 Attempt；本 Run 不应
                    # 伪造终态事件或继续让 Pi 调用任何工具。
                    await client.abort()
                    return RunStatus.CLARIFICATION_REQUESTED.value
                if event.get("type") == "agent_start" and not await self._count_decision(
                    run_id, attempt_id
                ):
                    await client.abort()
                    await self._flush_thinking(run_id)
                    await self._settle(
                        run_id,
                        user_id,
                        RunStatus.FAILED,
                        {"error_code": "pi_decision_limit"},
                    )
                    return RunStatus.FAILED.value
                if event.get("type") == "agent_end" and event.get("willRetry") is False:
                    return await self._complete_settled_run(run_id, user_id, assistant_parts)

            # Pi RPC 在已接受 prompt 后异常退出会由 PiRpcClient 抛出；这里收到自然
            # EOF 则同样不能把未确认 settled 的工作当成成功。
            await self._flush_thinking(run_id)
            await self._settle(
                run_id,
                user_id,
                RunStatus.FAILED,
                {"error_code": "pi_rpc_unsettled_exit"},
            )
            return RunStatus.FAILED.value
        except Exception as exc:  # noqa: BLE001 - subprocess/RPC 边界必须收口为唯一失败终态。
            await self._db.rollback()
            _DIAGNOSTICS_LOGGER.warning(
                "pi_poc_run_failure=%s",
                json.dumps(safe_db_diagnostic(exc), ensure_ascii=False, sort_keys=True),
            )
            await self._flush_thinking(run_id)
            await self._settle(
                run_id,
                user_id,
                RunStatus.FAILED,
                {
                    "error_code": exc.code
                    if isinstance(exc, PiRpcProtocolError)
                    else type(exc).__name__
                },
            )
            return RunStatus.FAILED.value
        finally:
            if client is not None:
                await client.close()

    def _buffer_thinking(self, text: str) -> None:
        self._thinking_parts.append(text)
        self._thinking_bytes += len(text.encode("utf-8"))

    async def _flush_thinking(self, run_id: str) -> None:
        if not self._thinking_parts:
            return
        text = "".join(self._thinking_parts)
        await PiRunAuditWriter(db=self._db, events=self._events).write_thinking_chunk(
            run_id=run_id,
            text=text,
        )
        self._thinking_parts.clear()
        self._thinking_bytes = 0

    async def _build_start_message(self, run_id: str) -> str:
        """重建受控上下文，不注入凭证、原始 Evidence 或业务工具路由。"""
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise LookupError("run_not_found")
        messages = (
            await self._db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == run.session_id)
                .order_by(AgentMessage.sequence.desc())
                .limit(20)
            )
        ).all()
        ordered = list(reversed(messages))
        session = await self._db.get(AgentSession, run.session_id)
        user_question = next(
            (message.content for message in ordered if message.id == run.input_message_id),
            "",
        )
        artifacts = (
            await self._db.execute(
                select(
                    AgentArtifact.id,
                    AgentArtifact.artifact_type,
                    AgentArtifactVersion.id,
                    AgentArtifactVersion.version,
                    AgentArtifactVersion.schema_version,
                    AgentArtifactVersion.data_status,
                )
                .join(AgentArtifactVersion, AgentArtifactVersion.artifact_id == AgentArtifact.id)
                .where(AgentArtifact.session_id == run.session_id, AgentArtifact.status == "published")
                .order_by(AgentArtifact.updated_at.desc(), AgentArtifactVersion.version.desc())
                .limit(50)
            )
        ).all()
        evidence = (
            await self._db.execute(
                select(
                    EvidenceItem.id,
                    EvidenceItem.source_name,
                    EvidenceItem.scope_json,
                    EvidenceItem.period_json,
                    EvidenceItem.availability_status,
                )
                .where(EvidenceItem.session_id == run.session_id)
                .order_by(EvidenceItem.collected_at.desc())
                .limit(100)
            )
        ).all()
        context = {
            "user_question": user_question,
            "recent_messages": [
                {"role": message.role, "content": message.content}
                for message in ordered
            ],
            "session_summary": session.session_summary if session is not None else None,
            "published_artifacts": [
                {
                    "artifact_id": row[0],
                    "artifact_type": row[1],
                    "version_id": row[2],
                    "version": row[3],
                    "schema_version": row[4],
                    "data_status": row[5],
                }
                for row in artifacts
            ],
            "evidence_index": [
                {
                    "evidence_id": row[0],
                    "source_name": row[1],
                    "scope": row[2],
                    "period": row[3],
                    "availability_status": row[4],
                }
                for row in evidence
            ],
            "runtime_time": {"utc": datetime.now(UTC).isoformat(), "timezone": "Asia/Shanghai"},
            "runtime_boundary": {
                "use_only_explicit_tools": True,
                "no_shell_or_file_tools": True,
                "no_secrets_in_output": True,
                "formal_outputs_require_builder_and_publish": True,
            },
        }
        return json.dumps(context, ensure_ascii=False)

    async def _audit_event(self, run_id: str, event: dict[str, Any]) -> None:
        await PiRunAuditWriter(db=self._db, events=self._events).write_rpc_event(
            run_id=run_id,
            event=event,
        )

    async def _count_decision(self, run_id: str, attempt_id: str) -> bool:
        locked = await AgentRunRepository(self._db).lock_run(run_id)
        attempt = await self._db.get(AgentRunAttempt, attempt_id)
        if attempt is None:
            raise LookupError("attempt_not_found")
        locked.decision_count += 1
        attempt.decision_count += 1
        await self._db.flush()
        await self._db.commit()
        return attempt.decision_count <= self._settings.pi_runtime_poc_max_decisions

    async def _cancel_requested(self, run_id: str) -> bool:
        run = await self._db.get(AgentRun, run_id, populate_existing=True)
        return run is not None and run.cancel_requested

    async def _clarification_requested(self, run_id: str) -> bool:
        run = await self._db.get(AgentRun, run_id, populate_existing=True)
        return run is not None and run.status == RunStatus.CLARIFICATION_REQUESTED

    async def _attempt_timed_out(self, attempt_id: str) -> bool:
        attempt = await self._db.get(AgentRunAttempt, attempt_id, populate_existing=True)
        if attempt is None:
            raise LookupError("attempt_not_found")
        elapsed = (datetime.now(UTC).replace(tzinfo=None) - attempt.started_at).total_seconds()
        return elapsed >= self._settings.pi_runtime_poc_run_timeout_seconds

    async def _complete_settled_run(
        self, run_id: str, user_id: str, assistant_parts: list[str]
    ) -> str:
        """仅 Pi 0.79 明确 ``agent_end/willRetry=false`` 后才允许走完成终态。"""
        text = "".join(assistant_parts).strip()
        if text:
            await self._events.append(run_id, user_id, "message.completed", {"text": text})
        published, has_restriction = await self._emit_published_artifact_events(run_id, user_id)
        outcome = (
            RunStatus.COMPLETED_WITH_WARNINGS
            if not text or has_restriction
            else RunStatus.COMPLETED
        )
        await self._settle(
            run_id,
            user_id,
            outcome,
            {"assistant_text_present": bool(text), "published_artifact_count": published},
        )
        return outcome.value

    async def _emit_published_artifact_events(self, run_id: str, user_id: str) -> tuple[int, bool]:
        """把已由确定性发布服务落库的版本投影为稳定 Artifact 事件。

        Pi 的工具执行结果不能作为发布事实；只有现有不可变 Version 行才可触发
        ``artifact.published``。因此未经发布的 Draft 永远不会进入产品事件或完成统计。
        """
        versions = (
            await self._db.execute(
                select(
                    AgentArtifactVersion.id,
                    AgentArtifactVersion.artifact_id,
                    AgentArtifactVersion.version,
                    AgentArtifactVersion.data_status,
                    AgentArtifact.module,
                )
                .join(AgentArtifact, AgentArtifact.id == AgentArtifactVersion.artifact_id)
                .where(AgentArtifactVersion.source_run_id == run_id)
                .order_by(AgentArtifactVersion.created_at)
            )
        ).all()
        emitted = (
            await self._db.scalars(
                select(AgentEvent.payload_json).where(
                    AgentEvent.run_id == run_id,
                    AgentEvent.event_type == "artifact.published",
                )
            )
        ).all()
        emitted_version_ids = {
            payload.get("artifact_version_id")
            for payload in emitted
            if isinstance(payload, dict) and isinstance(payload.get("artifact_version_id"), str)
        }
        for version_id, artifact_id, version, _data_status, module in versions:
            if version_id in emitted_version_ids:
                continue
            await self._events.append(
                run_id,
                user_id,
                "artifact.published",
                {
                    "artifact_id": artifact_id,
                    "artifact_version_id": version_id,
                    "version": version,
                    "module": module,
                },
            )
        return len(versions), any(data_status != "complete" for _, _, _, data_status, _ in versions)

    async def _settle(
        self, run_id: str, user_id: str, outcome: RunStatus, payload: dict[str, Any]
    ) -> None:
        await self._events.settle_terminal(
            run_id,
            user_id,
            outcome,
            payload,
            worker_id=self._worker_id,
        )


__all__ = ["PiClientFactory", "PiPocRunner", "PiRpcSession"]
