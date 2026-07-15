from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any

from app.model.contracts import (
    ModelAdapterError,
    ModelEvent,
    StreamingModelRequest,
    StructuredModelRequest,
    StructuredResult,
    T,
)


class FakeModelAdapter:
    """Scripted in-memory adapter used whenever the runtime provider is fake."""

    def __init__(
        self,
        *,
        structured_results: Iterable[StructuredResult[Any] | BaseException] = (),
        stream_results: Iterable[Iterable[ModelEvent] | BaseException] = (),
    ) -> None:
        self._structured_results = deque(structured_results)
        self._stream_results = deque(stream_results)
        self.structured_requests: list[StructuredModelRequest[Any]] = []
        self.streaming_requests: list[StreamingModelRequest] = []

    async def complete_json(
        self, request: StructuredModelRequest[T]
    ) -> StructuredResult[T]:
        self.structured_requests.append(request)
        if not self._structured_results:
            raise ModelAdapterError("FAKE_MODEL_NOT_SCRIPTED", retryable=False)
        result = self._structured_results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    async def stream_text(self, request: StreamingModelRequest):
        self.streaming_requests.append(request)
        if not self._stream_results:
            raise ModelAdapterError("FAKE_MODEL_NOT_SCRIPTED", retryable=False)
        result = self._stream_results.popleft()
        if isinstance(result, BaseException):
            raise result
        for event in result:
            yield event

    async def aclose(self) -> None:
        return None
