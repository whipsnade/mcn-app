"""统一 Tool Registry：工具契约、注册与 Profile/渠道权限过滤。

设计文档 §十「工具运行时」/ §16「安全与审计」。实际工具实现（MCP 桥、
历史读取、确定性计算、Artifact Draft）在后续 Task 8/9/12 落地。
"""

from app.agent_runtime.tools.contracts import (
    SERVER_RESERVED_KEYS,
    ToolContext,
    ToolResult,
    ToolStatus,
    TrustedTool,
)
from app.agent_runtime.tools.registry import (
    McpCatalogEntry,
    RegisteredTool,
    ToolContractError,
    ToolRegistry,
    UnknownToolError,
)

__all__ = [
    "McpCatalogEntry",
    "RegisteredTool",
    "SERVER_RESERVED_KEYS",
    "ToolContext",
    "ToolContractError",
    "ToolRegistry",
    "ToolResult",
    "ToolStatus",
    "TrustedTool",
    "UnknownToolError",
]
