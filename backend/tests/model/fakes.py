from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, BadRequestError


class FakeChatCompletions:
    """In-memory Chat Completions script; it never opens a network connection."""

    def __init__(self, results: list[Any]) -> None:
        self.results = deque(results)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def completion(
    content: str,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> Any:
    usage = None
    if any(value is not None for value in (prompt_tokens, completion_tokens, total_tokens)):
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=None),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=None),
        )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
        _request_id="request-test",
    )


def unsupported_schema_error(*, include_param: bool = True) -> BadRequestError:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    body = {
        "error": {
            "message": "response_format json_schema is not supported",
            "type": "invalid_request_error",
            "code": "unsupported_response_format",
        }
    }
    if include_param:
        body["error"]["param"] = "response_format"
    response = httpx.Response(400, request=request, json=body)
    return BadRequestError(body["error"]["message"], response=response, body=body)


def status_error(status_code: int, *, message: str = "provider error") -> APIStatusError:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    body = {"error": {"message": message, "type": "provider_error", "code": "provider_error"}}
    response = httpx.Response(status_code, request=request, json=body)
    return APIStatusError(message, response=response, body=body)


def connection_error() -> APIConnectionError:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    return APIConnectionError(request=request)


class ScriptedStream:
    def __init__(self, items: tuple[Any, ...]) -> None:
        self.items = deque(items)

    def __aiter__(self) -> "ScriptedStream":
        return self

    async def __anext__(self) -> Any:
        if not self.items:
            raise StopAsyncIteration
        item = self.items.popleft()
        if isinstance(item, BaseException):
            raise item
        return item


def chunk(*, content: str | None, finish_reason: str | None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
        _request_id="stream-request",
    )


def usage_chunk(
    *, prompt: int | None, completion: int | None, total: int | None
) -> Any:
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
        _request_id="stream-request",
    )


def adapter_with_stream(*items: Any):
    from app.model.tencent_plan import TencentPlanAdapter

    client = FakeChatCompletions([ScriptedStream(items)])
    return TencentPlanAdapter(client=client), client


def adapter_with_broken_stream(*, first_text: str):
    from app.model.tencent_plan import TencentPlanAdapter

    client = FakeChatCompletions(
        [
            ScriptedStream(
                (
                    chunk(content=first_text, finish_reason=None),
                    connection_error(),
                )
            )
        ]
    )
    return TencentPlanAdapter(client=client), client
