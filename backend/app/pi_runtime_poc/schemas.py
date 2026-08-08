"""Pi RPC POC 内部 HTTP 契约。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _PiPocSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PiToolStarted(_PiPocSchema):
    call_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    requested_tool_name: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.:-]{1,200}$"
    )
    service_name: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,80}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_payload: Any | None = None


class PiToolStartedResponse(_PiPocSchema):
    call_id: str


class PiToolSettled(_PiPocSchema):
    raw_payload: Any


class PiToolSettledResponse(_PiPocSchema):
    evidence_id: str | None = None


class PiToolFailed(_PiPocSchema):
    error: Any
    status: Literal["failed", "unknown"] = "failed"


PiExtensionStage = Literal[
    "config",
    "connect",
    "tools_list",
    "schema_validate",
    "tool_register",
    "audit_start",
    "mcp_call",
    "audit_settle",
]


class PiExtensionDiagnostic(_PiPocSchema):
    """Node Extension 的安全阶段诊断；绝不接收原始异常或请求数据。"""

    stage: PiExtensionStage
    service_slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,80}$")
    tool_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    exception_type: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,120}$")
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_:-]{1,120}$")


class PiSmokeRunFailed(_PiPocSchema):
    """单工具冒烟的终态收口只接受稳定错误码。"""

    code: str = Field(pattern=r"^[a-z0-9_:-]{1,120}$")


class PiInternalToolRequest(_PiPocSchema):
    tool_name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class PiInternalToolResponse(_PiPocSchema):
    result: Any


class PiRunContextRead(_PiPocSchema):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
