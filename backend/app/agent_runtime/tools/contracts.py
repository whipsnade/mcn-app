"""统一工具契约（设计文档 §十「工具运行时」/ §16「安全与审计」）。

每个可被 Agent 调用的工具（审核通过的 DataTap MCP 工具、历史读取、确定性
计算、Artifact Draft 工具）都遵循 :class:`TrustedTool` 协议，并返回统一的
:class:`ToolResult` 形状。

服务端上下文 ``user_id/session_id/run_id`` 由运行时通过 :class:`ToolContext`
注入（§16：「模型参数不能覆盖」）。出现在模型参数中的保留键在进入工具前被
剥离，见 :data:`SERVER_RESERVED_KEYS`。
"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

ToolStatus = Literal["success", "failed", "unknown"]


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


# 服务端保留键：模型参数中出现这些键一律在进入工具前被剥离。
SERVER_RESERVED_KEYS: frozenset[str] = frozenset({"user_id", "session_id", "run_id"})


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
    "SERVER_RESERVED_KEYS",
    "ToolContext",
    "ToolResult",
    "ToolStatus",
    "TrustedTool",
]
