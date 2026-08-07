"""Pi RPC POC 内部 HTTP 契约。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _PiPocSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PiToolStarted(_PiPocSchema):
    call_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
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
