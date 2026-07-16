from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict


T = TypeVar("T", bound=BaseModel)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class StructuredModelRequest(Generic[T]):
    purpose: Literal["planner", "replanner", "analyst", "followup"]
    template_name: str
    messages: tuple[ChatMessage, ...]
    output_model: type[T]
    max_tokens: int = 4096


class StreamingModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["summary"] = "summary"
    template_name: Literal["summary_v1"] = "summary_v1"
    messages: tuple[ChatMessage, ...]
    max_tokens: int = 2048


class ModelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text.delta", "usage.updated", "stream.completed"]
    text: str | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None


class StructuredResult(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    value: T
    usage: TokenUsage | None
    request_id: str | None
    regeneration_count: int


class ModelRequestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["planner", "replanner", "analyst", "summary", "followup"]
    provider: str
    model: str
    prompt_template: str
    prompt_version: str


class ModelAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


class ModelPlanInvalidError(ModelAdapterError):
    pass


class ModelStreamInterrupted(ModelAdapterError):
    def __init__(
        self,
        code: str = "MODEL_STREAM_INTERRUPTED",
        *,
        partial_output_received: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code, retryable=False, request_id=request_id)
        self.partial_output_received = partial_output_received


class ModelAdapter(Protocol):
    async def complete_json(
        self, request: StructuredModelRequest[T]
    ) -> StructuredResult[T]: ...

    def stream_text(self, request: StreamingModelRequest) -> AsyncIterator[ModelEvent]: ...

    async def aclose(self) -> None: ...
