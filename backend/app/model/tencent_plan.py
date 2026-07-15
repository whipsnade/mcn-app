from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from app.core.config import Settings
from app.model.contracts import (
    ModelAdapterError,
    ModelEvent,
    ModelPlanInvalidError,
    ModelStreamInterrupted,
    StreamingModelRequest,
    StructuredModelRequest,
    StructuredResult,
    T,
    TokenUsage,
)


CONFIRMED_BASE_URL = "https://tokenhub.tencentmaas.com/plan/v3"
CONFIRMED_MODEL = "deepseek-v4-pro-202606"
_SCHEMA_SUPPORT_CACHE: dict[tuple[str, str, str], bool] = {}
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_MAX_VALIDATION_ERRORS = 20
_MAX_VALIDATION_LOC_SEGMENTS = 8
_MAX_VALIDATION_LOC_SEGMENT_LENGTH = 64
_MAX_VALIDATION_TYPE_LENGTH = 64
_MAX_REPAIR_DETAILS_LENGTH = 1500
_STATUS_ERROR_CODES = {
    400: "MODEL_BAD_REQUEST",
    401: "MODEL_AUTHENTICATION_FAILED",
    402: "MODEL_QUOTA_EXCEEDED",
    403: "MODEL_AUTHENTICATION_FAILED",
    429: "MODEL_RATE_LIMITED",
    451: "MODEL_CONTENT_BLOCKED",
    499: "MODEL_CANCELLED",
    502: "MODEL_UPSTREAM_UNAVAILABLE",
    503: "MODEL_UPSTREAM_UNAVAILABLE",
    504: "MODEL_UPSTREAM_UNAVAILABLE",
}


class _ResponseFormatUnsupported(Exception):
    def __init__(self, request_id: str | None) -> None:
        self.request_id = request_id


def _value(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _request_id(source: Any) -> str | None:
    value = _value(source, "_request_id") or _value(source, "request_id")
    return str(value) if value else None


def _usage(source: Any) -> TokenUsage | None:
    raw = _value(source, "usage")
    if raw is None:
        return None
    prompt_details = _value(raw, "prompt_tokens_details")
    completion_details = _value(raw, "completion_tokens_details")
    return TokenUsage(
        prompt_tokens=_value(raw, "prompt_tokens"),
        completion_tokens=_value(raw, "completion_tokens"),
        total_tokens=_value(raw, "total_tokens"),
        cached_tokens=_value(prompt_details, "cached_tokens"),
        reasoning_tokens=_value(completion_details, "reasoning_tokens"),
    )


class TencentPlanAdapter:
    def __init__(
        self,
        *,
        client: Any,
        base_url: str = CONFIRMED_BASE_URL,
        model: str = CONFIRMED_MODEL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        max_attempts: int = 3,
        schema_support_cache: MutableMapping[tuple[str, str, str], bool] | None = None,
        owned_client: AsyncOpenAI | None = None,
    ) -> None:
        self._client = client
        self.base_url = base_url
        self.model = model
        self._sleep = sleep
        self._jitter = jitter
        self._max_attempts = max_attempts
        self._schema_support_cache = (
            schema_support_cache if schema_support_cache is not None else _SCHEMA_SUPPORT_CACHE
        )
        self._owned_client = owned_client

    @classmethod
    def from_settings(cls, settings: Settings) -> "TencentPlanAdapter":
        if settings.tencent_plan_api_key is None:
            raise ValueError("TENCENT_PLAN_API_KEY is required for Tencent model provider")
        client = AsyncOpenAI(
            api_key=settings.tencent_plan_api_key.get_secret_value(),
            base_url=settings.tencent_plan_base_url.unicode_string(),
            max_retries=0,
            timeout=settings.model_timeout_seconds,
        )
        return cls(
            client=client.chat.completions,
            base_url=settings.tencent_plan_base_url.unicode_string(),
            model=settings.tencent_plan_model,
            owned_client=client,
        )

    async def complete_json(
        self, request: StructuredModelRequest[T]
    ) -> StructuredResult[T]:
        schema = request.output_model.model_json_schema()
        digest = hashlib.sha256(
            json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cache_key = (self.base_url, self.model, digest)
        use_schema = self._schema_support_cache.get(cache_key) is not False
        messages = [message.model_dump() for message in request.messages]

        for regeneration_count in range(2):
            response_format = self._response_format(request, schema, use_schema=use_schema)
            try:
                response = await self._create_with_retry(
                    messages=messages,
                    max_tokens=request.max_tokens,
                    response_format=response_format,
                    detect_schema_unsupported=use_schema,
                )
            except _ResponseFormatUnsupported:
                self._schema_support_cache[cache_key] = False
                use_schema = False
                response = await self._create_with_retry(
                    messages=messages,
                    max_tokens=request.max_tokens,
                    response_format=self._response_format(request, schema, use_schema=False),
                    detect_schema_unsupported=False,
                )

            content = self._completion_content(response)
            try:
                value = request.output_model.model_validate_json(content, strict=True)
            except ValidationError as exc:
                if regeneration_count == 1:
                    raise ModelPlanInvalidError(
                        "MODEL_PLAN_INVALID",
                        retryable=False,
                        request_id=_request_id(response),
                    ) from exc
                messages = [*messages, self._repair_message(exc)]
                continue

            return StructuredResult[T](
                value=value,
                usage=_usage(response),
                request_id=_request_id(response),
                regeneration_count=regeneration_count,
            )

        raise AssertionError("unreachable")

    async def stream_text(self, request: StreamingModelRequest):
        create_attempt = 0
        while True:
            partial_output_received = False
            finish_reason: str | None = None
            request_id: str | None = None
            try:
                stream = await self._client.create(
                    model=self.model,
                    messages=[message.model_dump() for message in request.messages],
                    max_tokens=request.max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                async for chunk in stream:
                    request_id = _request_id(chunk) or request_id
                    usage = _usage(chunk)
                    if usage is not None:
                        yield ModelEvent(type="usage.updated", usage=usage)
                    choices = _value(chunk, "choices") or ()
                    for choice in choices:
                        reason = _value(choice, "finish_reason")
                        if reason is not None:
                            finish_reason = str(reason)
                        delta = _value(choice, "delta")
                        content = _value(delta, "content")
                        if content:
                            partial_output_received = True
                            yield ModelEvent(type="text.delta", text=str(content))
                if finish_reason is None:
                    raise ModelStreamInterrupted(
                        partial_output_received=partial_output_received,
                        request_id=request_id,
                    )
                yield ModelEvent(type="stream.completed", finish_reason=finish_reason)
                return
            except asyncio.CancelledError:
                raise
            except ModelStreamInterrupted:
                raise
            except Exception as exc:
                mapped = self._map_error(exc)
                if (
                    mapped.retryable
                    and not partial_output_received
                    and create_attempt + 1 < self._max_attempts
                ):
                    await self._backoff(create_attempt)
                    create_attempt += 1
                    continue
                if partial_output_received:
                    raise ModelStreamInterrupted(
                        partial_output_received=True,
                        request_id=mapped.request_id or request_id,
                    ) from exc
                raise mapped from exc

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.close()

    def _response_format(
        self,
        request: StructuredModelRequest[Any],
        schema: dict[str, Any],
        *,
        use_schema: bool,
    ) -> dict[str, Any]:
        if not use_schema:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": request.output_model.__name__,
                "strict": True,
                "schema": schema,
            },
        }

    async def _create_with_retry(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict[str, Any],
        detect_schema_unsupported: bool,
    ) -> Any:
        for attempt in range(self._max_attempts):
            try:
                return await self._client.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    stream=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if detect_schema_unsupported and self._is_schema_unsupported(exc):
                    raise _ResponseFormatUnsupported(_request_id(exc)) from exc
                mapped = self._map_error(exc)
                if not mapped.retryable or attempt + 1 >= self._max_attempts:
                    raise mapped from exc
                await self._backoff(attempt)
        raise AssertionError("unreachable")

    async def _backoff(self, attempt: int) -> None:
        await self._sleep((0.1 * (2**attempt)) + (0.05 * self._jitter()))

    def _map_error(self, exc: Exception) -> ModelAdapterError:
        if isinstance(exc, ModelAdapterError):
            return exc
        request_id = _request_id(exc)
        if isinstance(exc, (asyncio.TimeoutError, APITimeoutError)):
            return ModelAdapterError("MODEL_TIMEOUT", retryable=False, request_id=request_id)
        if isinstance(exc, APIStatusError):
            status = exc.status_code
            return ModelAdapterError(
                _STATUS_ERROR_CODES.get(status, "MODEL_UPSTREAM_ERROR"),
                retryable=status in _RETRYABLE_STATUS_CODES,
                request_id=request_id,
            )
        if isinstance(exc, APIConnectionError):
            return ModelAdapterError(
                "MODEL_NETWORK_ERROR",
                retryable=True,
                request_id=request_id,
            )
        return ModelAdapterError("MODEL_UPSTREAM_ERROR", retryable=False, request_id=request_id)

    def _is_schema_unsupported(self, exc: Exception) -> bool:
        if not isinstance(exc, APIStatusError) or exc.status_code != 400:
            return False
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body)
        if not isinstance(error, dict):
            return False
        param = str(error.get("param") or "").lower()
        code = str(error.get("code") or "").lower()
        message = str(error.get("message") or "").lower()
        if code in {"unsupported_response_format", "response_format_unsupported"}:
            return True
        explicitly_unsupported = "unsupported" in code or (
            "not supported" in message or "unsupported" in message
        )
        return param == "response_format" and explicitly_unsupported

    def _completion_content(self, response: Any) -> str:
        choices = _value(response, "choices") or ()
        if not choices:
            return ""
        message = _value(choices[0], "message")
        content = _value(message, "content")
        return content if isinstance(content, str) else ""

    def _repair_message(self, exc: ValidationError) -> dict[str, str]:
        safe_errors: list[dict[str, Any]] = []
        for error in exc.errors(include_url=False, include_input=False)[
            :_MAX_VALIDATION_ERRORS
        ]:
            error_type = error.get("type")
            safe_type = (
                error_type[:_MAX_VALIDATION_TYPE_LENGTH]
                if isinstance(error_type, str)
                else "validation_error"
            )
            safe_loc: list[str | int] = []
            loc = error.get("loc")
            if isinstance(loc, (list, tuple)):
                for segment in loc[:_MAX_VALIDATION_LOC_SEGMENTS]:
                    if isinstance(segment, str):
                        safe_loc.append(segment[:_MAX_VALIDATION_LOC_SEGMENT_LENGTH])
                    elif isinstance(segment, int):
                        safe_loc.append(segment)
                    else:
                        safe_loc.append("<unsupported>")
            safe_errors.append({"type": safe_type, "loc": safe_loc})
        compact = json.dumps(
            safe_errors,
            ensure_ascii=False,
            separators=(",", ":"),
        )[:_MAX_REPAIR_DETAILS_LENGTH]
        return {
            "role": "user",
            "content": (
                "The previous JSON failed validation. Return corrected JSON only, matching the "
                f"same schema. validation={compact}"
            ),
        }
