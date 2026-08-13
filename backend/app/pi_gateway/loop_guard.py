"""服务端持久化的跨 Attempt loop guard。

该 guard 只记录服务端计算出的稳定摘要，不接受模型传入的状态。Builder 错误和
``search_evidence`` 空转共享同一个 Run 行，因此 Attempt 重启、Gateway 重连或
Recovery 都不能把计数清零。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentMessage, AgentRun, AgentSession, EvidenceItem
from app.agent_runtime.tools.contracts import ToolResult
from app.mcp_gateway.validation import canonical_json_bytes

LOOP_GUARD_VERSION = 1
BUILDER_GUARD_THRESHOLD = 3
SEARCH_GUARD_THRESHOLD = 3
AGENT_LOOP_CIRCUIT_OPEN = "agent_loop_circuit_open"
BUILDER_ERROR_TYPES = frozenset(
    {
        "draft_build_error",
        "artifact_payload_invalid",
        "typed_artifact_requires_builder",
        "tool_arguments_invalid",
    }
)

_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DURABLE_NOT_VISIBLE = object()


def _empty_guard() -> dict[str, Any]:
    return {
        "version": LOOP_GUARD_VERSION,
        "builder": {
            "fingerprint": None,
            "evidence_set_version": None,
            "streak": 0,
        },
        "search_evidence": {
            "request_fingerprint": None,
            "evidence_set_version": None,
            "result_fingerprint": None,
            "streak": 0,
        },
        "terminal_code": None,
    }


def _stable_text(value: str) -> str:
    """去掉 UUID/时间戳后保留有限的稳定错误字段。"""
    value = _UUID_RE.sub("<uuid>", value)
    value = _TIMESTAMP_RE.sub("<timestamp>", value)
    return value[:512]


def _stable_value(value: Any) -> Any:
    if isinstance(value, str):
        return _stable_text(value)
    if isinstance(value, Mapping):
        return {str(key): _stable_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    return value


def error_fingerprint(tool_name: str, error_type: str | None, normalized_error: str) -> str:
    """对排序后的 ``{tool_name,error_type,normalized_error}`` 做 SHA-256。"""
    payload = {
        "error_type": error_type or "unknown",
        "normalized_error": _stable_text(normalized_error),
        "tool_name": tool_name,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _request_fingerprint(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(_stable_value(dict(arguments)))).hexdigest()


def _result_fingerprint(result: ToolResult) -> str:
    try:
        parsed: Any = json.loads(result.safe_summary)
    except (TypeError, ValueError):
        parsed = result.safe_summary
    stable = {
        "status": result.status,
        "error_type": result.error_type,
        "payload": _stable_value(parsed),
        "truncated": result.truncated,
    }
    return hashlib.sha256(canonical_json_bytes(stable)).hexdigest()


class LoopGuard:
    """更新/读取 ``agent_runs.loop_guard_json`` 的唯一服务端入口。"""

    def __init__(self, db: AsyncSession, *, durable_session_factory: Any = None) -> None:
        self.db = db
        # Production tool execution supplies an independent SessionFactory so
        # guard progress commits even when the long-running worker transaction
        # later crashes.  Tests and uncommitted fixtures intentionally fall
        # back to the caller session below.
        self._durable_session_factory = durable_session_factory

    async def reject_if_open(self, run: AgentRun) -> ToolResult | None:
        durable = await self._run_durable("reject_if_open", run)
        if durable is not _DURABLE_NOT_VISIBLE:
            return durable
        return await self._reject_if_open(run)

    async def _reject_if_open(self, run: AgentRun) -> ToolResult | None:
        locked = await self._lock_run(run.id)
        guard = self._guard(locked)
        self._sync(run, locked, guard)
        if guard["terminal_code"] != AGENT_LOOP_CIRCUIT_OPEN:
            return None
        return self._circuit_result()

    async def record_builder_result(
        self, run: AgentRun, tool_name: str, result: ToolResult
    ) -> ToolResult:
        durable = await self._run_durable(
            "record_builder_result", run, tool_name, result
        )
        if durable is not _DURABLE_NOT_VISIBLE:
            return durable
        return await self._record_builder_result(run, tool_name, result)

    async def _record_builder_result(
        self, run: AgentRun, tool_name: str, result: ToolResult
    ) -> ToolResult:
        locked = await self._lock_run(run.id)
        guard = self._guard(locked)
        self._sync(run, locked, guard)
        if guard["terminal_code"] == AGENT_LOOP_CIRCUIT_OPEN:
            return self._circuit_result()

        evidence_version = await self.evidence_set_version(locked.session_id)
        state = dict(guard["builder"])
        if result.status == "success":
            state = {
                "fingerprint": None,
                "evidence_set_version": evidence_version,
                "streak": 0,
            }
            guard["builder"] = state
            await self._persist(locked, guard, run)
            return result

        if result.error_type not in BUILDER_ERROR_TYPES:
            guard["builder"] = {
                "fingerprint": None,
                "evidence_set_version": evidence_version,
                "streak": 0,
            }
            await self._persist(locked, guard, run)
            return result

        fingerprint = error_fingerprint(tool_name, result.error_type, result.safe_summary)
        if (
            state.get("fingerprint") == fingerprint
            and state.get("evidence_set_version") == evidence_version
        ):
            streak = int(state.get("streak") or 0) + 1
        else:
            streak = 1
        guard["builder"] = {
            "fingerprint": fingerprint,
            "evidence_set_version": evidence_version,
            "streak": streak,
        }
        if streak >= BUILDER_GUARD_THRESHOLD:
            return await self._open(locked, guard, run)
        await self._persist(locked, guard, run)
        return result

    async def record_search_result(
        self, run: AgentRun, arguments: Mapping[str, Any], result: ToolResult
    ) -> ToolResult:
        durable = await self._run_durable(
            "record_search_result", run, arguments, result
        )
        if durable is not _DURABLE_NOT_VISIBLE:
            return durable
        return await self._record_search_result(run, arguments, result)

    async def _record_search_result(
        self, run: AgentRun, arguments: Mapping[str, Any], result: ToolResult
    ) -> ToolResult:
        locked = await self._lock_run(run.id)
        guard = self._guard(locked)
        self._sync(run, locked, guard)
        if guard["terminal_code"] == AGENT_LOOP_CIRCUIT_OPEN:
            return self._circuit_result()

        evidence_version = await self.evidence_set_version(locked.session_id)
        request_fp = _request_fingerprint(arguments)
        result_fp = _result_fingerprint(result)
        state = dict(guard["search_evidence"])
        if (
            state.get("request_fingerprint") == request_fp
            and state.get("result_fingerprint") == result_fp
            and state.get("evidence_set_version") == evidence_version
        ):
            streak = int(state.get("streak") or 0) + 1
        else:
            streak = 1
        guard["search_evidence"] = {
            "request_fingerprint": request_fp,
            "evidence_set_version": evidence_version,
            "result_fingerprint": result_fp,
            "streak": streak,
        }
        if streak >= SEARCH_GUARD_THRESHOLD:
            return await self._open(locked, guard, run)
        await self._persist(locked, guard, run)
        return result

    async def evidence_set_version(self, session_id: str) -> str:
        ids = list(
            (
                await self.db.scalars(
                    select(EvidenceItem.id)
                    .where(EvidenceItem.session_id == session_id)
                    .order_by(EvidenceItem.id)
                )
            ).all()
        )
        return hashlib.sha256(canonical_json_bytes(ids)).hexdigest()

    async def _run_durable(self, method_name: str, run: AgentRun, *args: Any) -> Any:
        """Run one guard operation in an independent committed transaction.

        A freshly-created Run may only be flushed in the caller transaction;
        that is normal for unit tests and for the creation boundary, so the
        caller remains the fallback until another connection can see the row.
        Once visible, the isolated session owns the lock order and commits the
        guard/explanation atomically without committing unrelated worker work.
        """
        if self._durable_session_factory is None:
            return _DURABLE_NOT_VISIBLE
        async with self._durable_session_factory() as durable_db:
            visible = await durable_db.scalar(
                select(AgentRun.id).where(AgentRun.id == run.id)
            )
            if visible is None:
                return _DURABLE_NOT_VISIBLE
            durable_guard = LoopGuard(durable_db)
            result = await getattr(durable_guard, f"_{method_name}")(run, *args)
            # The guarded operation only flushes its own transaction.  Commit
            # explicitly at this independent boundary; the caller's shared
            # Session must never be committed by a domain helper.
            await durable_db.commit()
            fresh = await durable_db.get(AgentRun, run.id)
            if fresh is not None:
                run.loop_guard_json = json.loads(
                    json.dumps(fresh.loop_guard_json, ensure_ascii=False)
                )
            return result

    async def _lock_run(self, run_id: str) -> AgentRun:
        # Match AgentRunRepository.begin_attempt/terminal lock ordering:
        # Session -> Run.  The session lock serializes explanation sequence
        # allocation and prevents a guard writer from deadlocking terminal or
        # Recovery code that already owns the session mutex.
        session_id = await self.db.scalar(
            select(AgentRun.session_id).where(AgentRun.id == run_id)
        )
        if session_id is None:
            raise LookupError("run_not_found")
        session = await self.db.scalar(
            select(AgentSession)
            .where(AgentSession.id == session_id)
            .with_for_update()
        )
        if session is None:
            raise LookupError("session_not_found")
        run = await self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if run is None:
            raise LookupError("run_not_found")
        return run

    @staticmethod
    def _guard(run: AgentRun) -> dict[str, Any]:
        raw = run.loop_guard_json
        if not isinstance(raw, dict) or raw.get("version") != LOOP_GUARD_VERSION:
            return _empty_guard()
        guard = _empty_guard()
        builder = raw.get("builder") if isinstance(raw.get("builder"), dict) else {}
        search = (
            raw.get("search_evidence")
            if isinstance(raw.get("search_evidence"), dict)
            else {}
        )
        guard["builder"] = {
            "fingerprint": LoopGuard._valid_fingerprint(builder.get("fingerprint")),
            "evidence_set_version": LoopGuard._valid_fingerprint(
                builder.get("evidence_set_version")
            ),
            "streak": LoopGuard._valid_streak(builder.get("streak")),
        }
        guard["search_evidence"] = {
            "request_fingerprint": LoopGuard._valid_fingerprint(
                search.get("request_fingerprint")
            ),
            "evidence_set_version": LoopGuard._valid_fingerprint(
                search.get("evidence_set_version")
            ),
            "result_fingerprint": LoopGuard._valid_fingerprint(
                search.get("result_fingerprint")
            ),
            "streak": LoopGuard._valid_streak(search.get("streak")),
        }
        terminal_code = raw.get("terminal_code")
        if terminal_code == AGENT_LOOP_CIRCUIT_OPEN:
            guard["terminal_code"] = terminal_code
        return guard

    @staticmethod
    def _valid_fingerprint(value: Any) -> str | None:
        return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None

    @staticmethod
    def _valid_streak(value: Any) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
            return value
        return 0

    @staticmethod
    def _save(locked: AgentRun, guard: dict[str, Any], original: AgentRun) -> None:
        locked.loop_guard_json = json.loads(json.dumps(guard, ensure_ascii=False))
        if original is not locked:
            original.loop_guard_json = locked.loop_guard_json

    async def _persist(
        self, locked: AgentRun, guard: dict[str, Any], original: AgentRun
    ) -> None:
        """Flush guard state; the owning transaction decides when to commit."""
        self._save(locked, guard, original)
        await self.db.flush()

    async def _open(
        self, locked: AgentRun, guard: dict[str, Any], original: AgentRun
    ) -> ToolResult:
        if guard["terminal_code"] != AGENT_LOOP_CIRCUIT_OPEN:
            guard["terminal_code"] = AGENT_LOOP_CIRCUIT_OPEN
            # Serialize the session sequence allocation with other assistant
            # message writers.  The guard explanation must be the sole durable
            # explanation even when two attempts open the circuit concurrently.
            session = await self.db.scalar(
                select(AgentSession)
                .where(AgentSession.id == locked.session_id)
                .with_for_update()
            )
            if session is None:
                raise LookupError("session_not_found")
            max_sequence = await self.db.scalar(
                select(func.max(AgentMessage.sequence)).where(
                    AgentMessage.session_id == locked.session_id
                )
            )
            self.db.add(
                AgentMessage(
                    id=str(uuid4()),
                    session_id=locked.session_id,
                    run_id=locked.id,
                    role="assistant",
                    content=(
                        "系统已停止重复的产物校验/证据检索循环（agent_loop_circuit_open）。"
                        "当前证据集合没有产生足以修复相同错误的变化，请调整查询或补充有效证据后重试。"
                    ),
                    metadata_json={
                        "system_loop_guard": True,
                        "terminal_code": AGENT_LOOP_CIRCUIT_OPEN,
                    },
                    sequence=(max_sequence or 0) + 1,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        await self._persist(locked, guard, original)
        return self._circuit_result()

    @staticmethod
    def _sync(original: AgentRun, locked: AgentRun, guard: dict[str, Any]) -> None:
        if original is not locked:
            original.loop_guard_json = locked.loop_guard_json

    @staticmethod
    def _circuit_result() -> ToolResult:
        return ToolResult(
            status="failed",
            safe_summary=(
                "agent_loop_circuit_open: repeated identical builder/search result; "
                "stop retrying and adjust the evidence/query"
            ),
            error_type=AGENT_LOOP_CIRCUIT_OPEN,
        )


__all__ = [
    "AGENT_LOOP_CIRCUIT_OPEN",
    "BUILDER_GUARD_THRESHOLD",
    "LOOP_GUARD_VERSION",
    "LoopGuard",
    "SEARCH_GUARD_THRESHOLD",
    "error_fingerprint",
]
