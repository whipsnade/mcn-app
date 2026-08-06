"""统一工具契约（设计文档 §十「工具运行时」/ §16「安全与审计」）。

每个可被 Agent 调用的工具（审核通过的 DataTap MCP 工具、历史读取、确定性
计算、Artifact Draft 工具）都遵循 :class:`TrustedTool` 协议，并返回统一的
:class:`ToolResult` 形状。

服务端上下文 ``user_id/session_id/run_id`` 由运行时通过 :class:`ToolContext`
注入（§16：「模型参数不能覆盖」）。出现在模型参数中的保留键在进入工具前被
剥离，见 :data:`SERVER_RESERVED_KEYS`。
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from app.mcp_gateway.validation import canonical_json_bytes

ToolStatus = Literal["success", "failed", "unknown"]

# 工具执行前参数校验失败的结构化错误分类（registry 统一回喂）。语义与 MCP 侧
# definitely_not_sent 对齐：校验在 dispatch 之前失败，工具零副作用、零计费，
# 模型拿字段级明细自愈后重试。
TOOL_ARGUMENTS_INVALID = "tool_arguments_invalid"

# 结构化错误回喂的长度上限：字段级明细足够模型定位问题即可，绝不撑爆上下文。
ERROR_SUMMARY_LIMIT = 2000


def truncate_summary(text: str, limit: int = ERROR_SUMMARY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def format_validation_error(
    exc: ValidationError, *, prefix: str, limit: int = ERROR_SUMMARY_LIMIT
) -> str:
    """Pydantic 校验失败 → 字段级明细（``loc: msg [type]``），截断到上限。"""
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {error.get('msg')} [{error.get('type')}]")
    return truncate_summary(prefix + "; ".join(parts), limit)


class ToolResult(BaseModel):
    """统一工具结果形状。

    - ``safe_summary``：进入模型上下文的脱敏摘要；
    - ``evidence_id``：完整原始结果落证据库后的证据 ID，可为空；
    - ``cursor``：大结果的后续读取游标，可为空；
    - ``truncated``：原始结果是否被截断；
    - ``error_type``：结构化错误分类（对应 §11.1 三种故障分类），可为空。
    """

    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    safe_summary: str
    evidence_id: str | None = None
    cursor: str | None = None
    truncated: bool = False
    error_type: str | None = None


class ToolContext(BaseModel):
    """服务端注入的调用上下文；模型不能构造或覆盖（§16）。"""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    session_id: str
    run_id: str
    profile_name: str
    # MCP 桥挂靠 agent_tool_calls 所需；非 MCP 工具可为 None。
    step_id: str | None = None


# 服务端保留键：模型参数中出现这些键一律在进入工具前被剥离。
SERVER_RESERVED_KEYS: frozenset[str] = frozenset(
    {"user_id", "session_id", "run_id", "step_id"}
)


def arguments_hash(normalized_arguments: Mapping[str, Any]) -> str:
    """参数先按工具 Schema 归一化，再 canonical JSON + SHA-256。"""
    return hashlib.sha256(canonical_json_bytes(normalized_arguments)).hexdigest()


def logical_call_id_for(
    run_id: str, step_id: str, internal_tool_name: str, arguments_hash: str
) -> str:
    """确定性派生全局唯一 logical_call_id（§8.1）。"""
    raw = "\x00".join((run_id, step_id, internal_tool_name, arguments_hash))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TrustedTool(Protocol):
    """可信工具协议。

    结构检查（字段类型、input_model 是否为 Pydantic 模型、execute 是否异步、
    是否声明服务端保留键）在 ``registry.register`` 处执行。
    """

    name: str
    input_model: type[BaseModel]
    points_cost: int
    external_side_effect: bool

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult: ...


__all__ = [
    "ERROR_SUMMARY_LIMIT",
    "SERVER_RESERVED_KEYS",
    "TOOL_ARGUMENTS_INVALID",
    "ToolContext",
    "ToolResult",
    "ToolStatus",
    "TrustedTool",
    "format_validation_error",
    "truncate_summary",
]
