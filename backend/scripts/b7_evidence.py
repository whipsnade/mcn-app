"""REAL B7 UAT 证据写入器：canonical JSONL hash chain + 严格 DTO 校验。

设计约束（2026-08-12 架构复核后固化）：

- 禁止 positional tuple/index 拼装证据：一律使用命名字段、ORM 属性或
  ``row._mapping``；本模块的 builder 只接受命名输入。
- 证据 DTO 全部是 strict pydantic 模型（``extra="forbid"``），字段语义在
  写入前完成校验（terminal 集合、error_code 形态、账务恒等式等）。
- 追加型文件只允许 append → flush → fsync；写错的帧只能用 correction
  帧引用原帧 sequence 更正，禁止修改/删除/重排既有帧。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

ZERO_HASH = "0" * 64

JSONL_FILES = frozenset(
    {
        "manifest.jsonl",
        "scenario-results.jsonl",
        "run-identities.jsonl",
        "accounting-summary.jsonl",
        "usage-reconciliation.jsonl",
        "artifact-lineage.jsonl",
        "event-ordering.jsonl",
        "security-scan.jsonl",
    }
)

#: 终态事件的封闭集合；``run.started`` 等生命周期事件永远不得计入。
TERMINAL_EVENT_TYPES = frozenset(
    {"run.completed", "run.completed_with_warnings", "run.failed", "run.cancelled"}
)

RUN_TERMINAL_STATUSES = (
    "completed",
    "completed_with_warnings",
    "failed",
    "cancelled",
    "clarification_requested",
)

_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def canonical_bytes(obj: object) -> bytes:
    """Canonical JSON：键按 ASCII 排序、紧凑分隔符、非 ASCII 不转义、UTF-8。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload_dict(payload: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return dict(payload)


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunIdentityPayload(StrictPayload):
    run_id: str
    session_id: str
    tenant_id: str
    user_id: str
    backend: Literal["current", "pi"]
    runtime_config_version_id: str | None
    terminal: Literal[
        "completed", "completed_with_warnings", "failed", "cancelled", "clarification_requested"
    ]
    snapshot_binding_ok: bool
    attempts: int
    model: str | None = None

    @model_validator(mode="after")
    def _check_pi_binding(self) -> RunIdentityPayload:
        if self.backend == "pi" and not self.runtime_config_version_id:
            raise ValueError("pi_backend_requires_config_version")
        if self.attempts < 1:
            raise ValueError("attempts_must_be_positive")
        return self


class EventOrderPayload(StrictPayload):
    run_id: str
    sequence_monotonic: bool
    terminal_count: int
    terminal_types: list[str]
    message_completed_count: int
    message_completed_before_terminal: bool

    @field_validator("terminal_types")
    @classmethod
    def _terminal_closed_set(cls, value: list[str]) -> list[str]:
        unknown = [item for item in value if item not in TERMINAL_EVENT_TYPES]
        if unknown:
            raise ValueError(f"non_terminal_event_in_terminal_set:{unknown[0]}")
        return value

    @model_validator(mode="after")
    def _count_matches(self) -> EventOrderPayload:
        if self.terminal_count != len(self.terminal_types):
            raise ValueError("terminal_count_mismatch")
        return self


class ScenarioResultPayload(StrictPayload):
    scenario: str
    verdict: Literal["PASS", "FAIL", "BLOCKED"]
    terminal: str | None
    error_code: str | None = None
    model_requests: int = 0
    mcp_outbound: int = 0
    points_charged: int = 0
    detail: str | None = None

    @field_validator("terminal")
    @classmethod
    def _terminal_known(cls, value: str | None) -> str | None:
        if value is not None and value not in RUN_TERMINAL_STATUSES and value != "NOT_APPLICABLE":
            raise ValueError(f"unknown_terminal:{value}")
        return value

    @field_validator("error_code")
    @classmethod
    def _error_code_is_stable_code(cls, value: str | None) -> str | None:
        # error_code 只允许稳定 snake_case 码；模型名（如 deepseek-v4-pro，含 '-'）
        # 或任意自由文本写进来即拒绝。
        if value is not None and not _STABLE_CODE.match(value):
            raise ValueError(f"error_code_not_stable:{value!r}")
        return value


class AccountingPayload(StrictPayload):
    run_id: str
    net_points: int
    confirmed_outbound: int
    wallet_balance_after: int
    wallet_reserved_after: int

    @model_validator(mode="after")
    def _wallet_identity(self) -> AccountingPayload:
        if self.net_points != 10 * self.confirmed_outbound:
            raise ValueError("wallet_identity_violated")
        if self.wallet_reserved_after < 0 or self.wallet_balance_after < 0:
            raise ValueError("wallet_negative")
        return self


class UsageRecordPayload(StrictPayload):
    id: str
    kind: str
    backend: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    usage_status: str
    cost_status: str


class ToolCallPayload(StrictPayload):
    id: str
    internal_tool_name: str
    service: str
    status: str
    points_settled: int | None
    logical_call_id: str


def run_identity_from_orm(
    run: Any,
    *,
    expected_config_version_id: str | None,
    attempts: int,
) -> RunIdentityPayload:
    """从 AgentRun ORM 对象按命名属性构造 run identity（禁止位置索引）。"""
    expected = expected_config_version_id
    return RunIdentityPayload(
        run_id=run.id,
        session_id=run.session_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        backend=run.runtime_backend,
        runtime_config_version_id=run.runtime_config_version_id,
        terminal=run.status,
        snapshot_binding_ok=bool(expected) and run.runtime_config_version_id == expected,
        attempts=attempts,
        model=getattr(run, "model", None),
    )


def tool_call_from_orm(call: Any) -> ToolCallPayload:
    return ToolCallPayload(
        id=call.id,
        internal_tool_name=call.internal_tool_name,
        service=call.service,
        status=call.status,
        points_settled=call.points_settled,
        logical_call_id=call.logical_call_id,
    )


def usage_record_from_orm(record: Any) -> UsageRecordPayload:
    return UsageRecordPayload(
        id=record.id,
        kind=record.kind,
        backend=record.backend,
        model=record.model,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        usage_status=record.usage_status,
        cost_status=record.cost_status,
    )


def event_order_summary(run_id: str, events: Sequence[Any]) -> EventOrderPayload:
    """事件顺序摘要；``events`` 元素只需提供命名属性 ``sequence``/``event_type``
    （ORM 对象或 ``row._mapping`` 均可）。terminal 只计封闭集合内的 run.* 事件。
    """
    sequences = [int(e.sequence) for e in events]
    terminals = [e.event_type for e in events if e.event_type in TERMINAL_EVENT_TYPES]
    message_completed = [int(e.sequence) for e in events if e.event_type == "message.completed"]
    before = bool(
        message_completed
        and terminals
        and message_completed[-1] < max(int(e.sequence) for e in events if e.event_type in TERMINAL_EVENT_TYPES)
    )
    return EventOrderPayload(
        run_id=run_id,
        sequence_monotonic=sequences == sorted(sequences) and len(set(sequences)) == len(sequences),
        terminal_count=len(terminals),
        terminal_types=list(terminals),
        message_completed_count=len(message_completed),
        message_completed_before_terminal=before,
    )


def check_cross_file_consistency(
    *,
    run_identity: RunIdentityPayload,
    scenario_result: ScenarioResultPayload,
    accounting: AccountingPayload,
    usage_records: Sequence[UsageRecordPayload],
) -> list[str]:
    """跨文件一致性校验；返回错误码列表（空 = 通过）。"""
    errors: list[str] = []
    if scenario_result.terminal != run_identity.terminal:
        errors.append("scenario_terminal_mismatch")
    if scenario_result.model_requests != len(usage_records):
        errors.append("model_requests_mismatch")
    if accounting.net_points != 10 * accounting.confirmed_outbound:
        errors.append("wallet_identity_mismatch")
    if not run_identity.snapshot_binding_ok:
        errors.append("snapshot_binding_mismatch")
    if (
        scenario_result.error_code is not None
        and run_identity.model is not None
        and scenario_result.error_code == run_identity.model
    ):
        errors.append("error_code_contains_model_name")
    return errors


class EvidenceWriter:
    """append-only canonical JSONL hash-chain 写入器（flush + fsync 逐帧）。

    使用约束：单 operator 顺序写入（B7 round 协议即为串行）；read-last-then-
    append 非原子，多写入者并发会分叉链。帧级 fsync 保证每帧落盘；新建文件
    的目录项持久化依赖文件系统语义，round 封口由 hashes.sha256 总体锚定。
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    def _path(self, name: str) -> Path:
        if name not in JSONL_FILES:
            raise ValueError(f"unknown_jsonl_evidence_file:{name}")
        return self._dir / name

    def _last(self, name: str) -> tuple[int, str]:
        path = self._path(name)
        if not path.exists():
            return 0, ZERO_HASH
        sequence = 0
        last = ZERO_HASH
        with path.open("rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                sequence += 1
                last = json.loads(line.decode("utf-8"))["record_hash"]
        return sequence, last

    def append(
        self,
        name: str,
        *,
        scenario_id: str,
        type: str,
        payload: BaseModel | Mapping[str, Any],
    ) -> dict[str, Any]:
        sequence, prev_hash = self._last(name)
        frame: dict[str, Any] = {
            "sequence": sequence + 1,
            "timestamp": utc_now(),
            "scenario_id": scenario_id,
            "type": type,
            "payload": _payload_dict(payload),
            "prev_hash": prev_hash,
        }
        frame["record_hash"] = hashlib.sha256(canonical_bytes(frame)).hexdigest()
        line = canonical_bytes(frame) + b"\n"
        path = self._path(name)
        with path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return frame

    def correction(
        self,
        name: str,
        *,
        scenario_id: str,
        corrects_sequence: int,
        note: str,
        payload: BaseModel | Mapping[str, Any],
    ) -> dict[str, Any]:
        """追加更正帧：引用原帧 sequence，原帧保留不变。"""
        return self.append(
            name,
            scenario_id=scenario_id,
            type="correction",
            payload={
                "corrects_sequence": corrects_sequence,
                "note": note,
                "payload": _payload_dict(payload),
            },
        )

    def verify(self, name: str, *, expected_head: str | None = None) -> list[str]:
        """逐帧校验 canonical/sequence/prev_hash/record_hash。

        注意：仅逐帧校验无法发现「尾部帧被删除」（剩余帧自洽）；因此支持
        ``expected_head`` 外部锚点（如 round_sealed/verdict 记录的链头），
        传入后尾删会报 ``head_mismatch``。
        """
        errors: list[str] = []
        path = self._path(name)
        if not path.exists():
            return [f"{name}:missing"]
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            errors.append(f"{name}:missing_trailing_lf")
        lines = [ln for ln in raw.split(b"\n") if ln]
        prev = ZERO_HASH
        for index, line in enumerate(lines, start=1):
            try:
                frame = json.loads(line.decode("utf-8"))
            except Exception:  # noqa: BLE001
                errors.append(f"{name}:{index}:unparseable")
                continue
            if canonical_bytes(frame) != line:
                errors.append(f"{name}:{index}:non_canonical")
            if frame.get("sequence") != index:
                errors.append(f"{name}:{index}:sequence_mismatch")
            if frame.get("prev_hash") != prev:
                errors.append(f"{name}:{index}:prev_hash_mismatch")
            body = {k: v for k, v in frame.items() if k != "record_hash"}
            if frame.get("record_hash") != hashlib.sha256(canonical_bytes(body)).hexdigest():
                errors.append(f"{name}:{index}:record_hash_mismatch")
            prev = frame.get("record_hash", prev)
        if expected_head is not None and prev != expected_head:
            errors.append(f"{name}:head_mismatch")
        return errors

    def head(self, name: str) -> tuple[int, str]:
        """返回 (帧数, 链头 record_hash)；空文件/不存在返回 (0, ZERO_HASH)。"""
        return self._last(name)

    def verify_all(self) -> list[str]:
        errors: list[str] = []
        for name in sorted(JSONL_FILES):
            if self._path(name).exists():
                errors.extend(self.verify(name))
        return errors
