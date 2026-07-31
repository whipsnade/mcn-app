from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
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
    ThinkingSink,
    TokenUsage,
)
from app.model.prompt_logs import PromptLogEntry, PromptLogWriter
from app.model.structured_output import (
    ParsedStructuredOutput,
    ThinkJsonStreamParser,
    parse_non_stream_output,
    validate_with_repair,
)


CONFIRMED_BASE_URL = "https://tokenhub.tencentmaas.com/plan/v3"
CONFIRMED_MODEL = "deepseek-v4-pro"
_SCHEMA_SUPPORT_CACHE: dict[tuple[str, str, str], bool] = {}
_STREAM_SUPPORT_CACHE: dict[tuple[str, str], bool] = {}
logger = logging.getLogger(__name__)
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


class _StreamUnsupported(Exception):
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


class _PromptLogState:
    """单次模型调用的日志累积状态（实际发送的消息、响应文本、用量）。"""

    __slots__ = ("purpose", "log_context", "started", "messages", "parts", "usage")

    def __init__(self, purpose: str, log_context: dict[str, Any] | None) -> None:
        self.purpose = purpose
        self.log_context = log_context or {}
        self.started = time.monotonic()
        self.messages: list[dict[str, str]] = []
        self.parts: list[str] = []
        self.usage: TokenUsage | None = None

    @property
    def response_text(self) -> str:
        return "".join(self.parts)


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
        stream_support_cache: MutableMapping[tuple[str, str], bool] | None = None,
        owned_client: AsyncOpenAI | None = None,
        log_writer: PromptLogWriter | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._client = client
        self.base_url = base_url
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._sleep = sleep
        self._jitter = jitter
        self._max_attempts = max_attempts
        self._schema_support_cache = (
            schema_support_cache if schema_support_cache is not None else _SCHEMA_SUPPORT_CACHE
        )
        self._stream_support_cache = (
            stream_support_cache if stream_support_cache is not None else _STREAM_SUPPORT_CACHE
        )
        self._owned_client = owned_client
        # None = 使用默认写库实现（惰性导入，避免模块 import 期触碰数据库配置）。
        self._log_writer = log_writer

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
            reasoning_effort=settings.tencent_plan_reasoning_effort,
        )

    async def complete_json(
        self, request: StructuredModelRequest[T]
    ) -> StructuredResult[T]:
        log = _PromptLogState(request.purpose, request.log_context)
        try:
            result = await self._complete_json(request, log)
        except ModelPlanInvalidError as exc:
            await self._emit_log(log, status="invalid", error_code=exc.code)
            raise
        except ModelAdapterError as exc:
            await self._emit_log(log, status="failed", error_code=exc.code)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_log(log, status="failed", error_code=type(exc).__name__)
            raise
        await self._emit_log(log, status="success", error_code=None, usage=result.usage)
        return result

    async def _complete_json(
        self, request: StructuredModelRequest[T], log: _PromptLogState
    ) -> StructuredResult[T]:
        schema = request.output_model.model_json_schema()
        digest = hashlib.sha256(
            json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cache_key = (self.base_url, self.model, digest)
        # 腾讯 Token Plan 的 DeepSeek-V4-Pro 仅支持 OpenAI 兼容的 json_object；
        # 预先探测 json_schema 会直接造成 400，且会使任务规划产生不必要的失败点。
        use_schema = False
        messages = [message.model_dump() for message in request.messages]
        if not use_schema:
            schema_instruction = {
                "role": "system",
                "content": (
                    "仅输出一个满足以下 JSON Schema 的 JSON 对象；不得输出解释、Markdown 或额外字段。"
                    "\nJSON Schema:\n"
                    + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                ),
            }
            messages = [messages[0], schema_instruction, *messages[1:]]
        log.messages = list(messages)

        if request.thinking_sink is not None:
            return await self._complete_json_stream(
                request=request,
                log=log,
                schema=schema,
                messages=messages,
                use_schema=use_schema,
            )

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
            log.parts = [content]
            log.usage = _usage(response)
            try:
                parsed = parse_non_stream_output(content)
                value = validate_with_repair(request.output_model, parsed.json_text)
            except (ValidationError, ValueError) as exc:
                if regeneration_count == 1:
                    raise ModelPlanInvalidError(
                        "MODEL_PLAN_INVALID",
                        retryable=False,
                        request_id=_request_id(response),
                    ) from exc
                messages = [*messages, self._repair_message(exc)]
                log.messages = list(messages)
                continue

            return StructuredResult[T](
                value=value,
                usage=_usage(response),
                request_id=_request_id(response),
                regeneration_count=regeneration_count,
            )

        raise AssertionError("unreachable")

    async def _complete_json_stream(
        self,
        *,
        request: StructuredModelRequest[T],
        log: _PromptLogState,
        schema: dict[str, Any],
        messages: list[dict[str, str]],
        use_schema: bool,
    ) -> StructuredResult[T]:
        sink = request.thinking_sink
        assert sink is not None
        stream_cache_key = (self.base_url, self.model)

        for regeneration_count in range(2):
            attempt = regeneration_count + 1
            started = time.monotonic()
            await self._safe_sink_call(sink, "started", attempt=attempt)
            response_format = self._response_format(request, schema, use_schema=use_schema)
            log.parts = []
            request_id: str | None = None
            try:
                if self._stream_support_cache.get(stream_cache_key, True):
                    try:
                        parsed, usage, request_id = await self._create_json_stream_with_retry(
                            messages=messages,
                            max_tokens=request.max_tokens,
                            response_format=response_format,
                            sink=sink,
                            attempt=attempt,
                            log=log,
                        )
                    except _StreamUnsupported:
                        self._stream_support_cache[stream_cache_key] = False
                        parsed, usage, request_id = await self._create_json_non_stream_for_sink(
                            messages=messages,
                            max_tokens=request.max_tokens,
                            response_format=response_format,
                            sink=sink,
                            attempt=attempt,
                            log=log,
                        )
                else:
                    parsed, usage, request_id = await self._create_json_non_stream_for_sink(
                        messages=messages,
                        max_tokens=request.max_tokens,
                        response_format=response_format,
                        sink=sink,
                        attempt=attempt,
                        log=log,
                    )

                value = validate_with_repair(request.output_model, parsed.json_text)
            except asyncio.CancelledError:
                # Sink 已 started 后被取消也必须给出失败终态，
                # 否则运行中的 operation 快照会永久残留。
                await self._safe_sink_call(
                    sink,
                    "failed",
                    attempt=attempt,
                    error_code="CANCELLED",
                )
                raise
            except (ValidationError, ValueError) as exc:
                await self._safe_sink_call(
                    sink,
                    "failed",
                    attempt=attempt,
                    error_code="MODEL_PLAN_INVALID",
                )
                if regeneration_count == 1:
                    raise ModelPlanInvalidError(
                        "MODEL_PLAN_INVALID",
                        retryable=False,
                        request_id=request_id,
                    ) from exc
                messages = [*messages, self._repair_message(exc)]
                log.messages = list(messages)
                continue
            except ModelAdapterError as exc:
                await self._safe_sink_call(
                    sink,
                    "failed",
                    attempt=attempt,
                    error_code=exc.code,
                )
                raise
            except Exception as exc:
                mapped = self._map_error(exc)
                await self._safe_sink_call(
                    sink,
                    "failed",
                    attempt=attempt,
                    error_code=mapped.code,
                )
                raise mapped from exc

            await self._safe_sink_call(
                sink,
                "completed",
                attempt=attempt,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return StructuredResult[T](
                value=value,
                usage=usage,
                request_id=request_id,
                regeneration_count=regeneration_count,
            )

        raise AssertionError("unreachable")

    async def _create_json_non_stream_for_sink(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict[str, Any],
        sink: ThinkingSink,
        attempt: int,
        log: _PromptLogState,
    ) -> tuple[ParsedStructuredOutput, TokenUsage | None, str | None]:
        response = await self._create_with_retry(
            messages=messages,
            max_tokens=max_tokens,
            response_format=response_format,
            detect_schema_unsupported=False,
        )
        content = self._completion_content(response)
        log.parts = [content]
        usage = _usage(response)
        log.usage = usage
        parser = ThinkJsonStreamParser()
        for text in parser.feed_content(content):
            await self._safe_sink_call(sink, "delta", text, attempt=attempt)
        parsed = parser.finish()
        return parsed, usage, _request_id(response)

    async def _create_json_stream_with_retry(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict[str, Any],
        sink: ThinkingSink,
        attempt: int,
        log: _PromptLogState,
    ) -> tuple[ParsedStructuredOutput, TokenUsage | None, str | None]:
        create_attempt = 0
        while True:
            parser = ThinkJsonStreamParser()
            partial_output_received = False
            finish_reason: str | None = None
            request_id: str | None = None
            usage: TokenUsage | None = None
            try:
                stream = await self._client.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    stream=True,
                    stream_options={"include_usage": True},
                    **self._create_kwargs(),
                )
                async for chunk in stream:
                    request_id = _request_id(chunk) or request_id
                    chunk_usage = _usage(chunk)
                    if chunk_usage is not None:
                        usage = chunk_usage
                        log.usage = chunk_usage
                    choices = _value(chunk, "choices") or ()
                    for choice in choices:
                        reason = _value(choice, "finish_reason")
                        if reason is not None:
                            finish_reason = str(reason)
                        delta = _value(choice, "delta")
                        reasoning = _value(delta, "reasoning_content")
                        if reasoning:
                            partial_output_received = True
                            for text in parser.feed_reasoning(str(reasoning)):
                                await self._safe_sink_call(sink, "delta", text, attempt=attempt)
                        content = _value(delta, "content")
                        if content:
                            partial_output_received = True
                            text_content = str(content)
                            log.parts.append(text_content)
                            for text in parser.feed_content(text_content):
                                await self._safe_sink_call(sink, "delta", text, attempt=attempt)
                if finish_reason is None:
                    raise ModelStreamInterrupted(
                        partial_output_received=partial_output_received,
                        request_id=request_id,
                    )
                return parser.finish(), usage, request_id
            except asyncio.CancelledError:
                raise
            except ValueError:
                raise
            except ModelStreamInterrupted:
                raise
            except Exception as exc:
                if not partial_output_received and self._is_stream_unsupported(exc):
                    raise _StreamUnsupported(_request_id(exc) or request_id) from exc
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

    async def _safe_sink_call(
        self,
        sink: ThinkingSink,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        try:
            await getattr(sink, method)(*args, **kwargs)
        except Exception:
            logger.warning("thinking sink failed method=%s", method, exc_info=True)

    async def stream_text(self, request: StreamingModelRequest):
        log = _PromptLogState(request.purpose, request.log_context)
        log.messages = [message.model_dump() for message in request.messages]
        status, error_code = "success", None
        cancelled = False
        try:
            async for event in self._stream_text(request, log):
                yield event
        except asyncio.CancelledError:
            cancelled = True
            raise
        except ModelAdapterError as exc:
            status, error_code = "failed", exc.code
            raise
        except Exception as exc:
            status, error_code = "failed", type(exc).__name__
            raise
        finally:
            if not cancelled:
                await self._emit_log(log, status=status, error_code=error_code)

    async def _stream_text(self, request: StreamingModelRequest, log: _PromptLogState):
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
                    **self._create_kwargs(),
                )
                async for chunk in stream:
                    request_id = _request_id(chunk) or request_id
                    usage = _usage(chunk)
                    if usage is not None:
                        log.usage = usage
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
                            log.parts.append(str(content))
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

    async def _emit_log(
        self,
        log: _PromptLogState,
        *,
        status: str,
        error_code: str | None,
        usage: TokenUsage | None = None,
    ) -> None:
        """写 prompt 学习日志；任何失败只记 warning，绝不阻塞主流程。"""
        writer = self._log_writer
        if writer is None:
            from app.model.prompt_logs import record_prompt_log

            writer = record_prompt_log
        effective_usage = usage or log.usage
        context = log.log_context
        tags = context.get("tags") or ()
        entry = PromptLogEntry(
            purpose=log.purpose,
            model=self.model,
            messages=json.dumps(log.messages, ensure_ascii=False),
            response=log.response_text or None,
            status=status,
            error_code=error_code,
            user_id=context.get("user_id"),
            session_id=context.get("session_id"),
            task_id=context.get("task_id"),
            tags=tuple(str(tag) for tag in tags),
            prompt_tokens=effective_usage.prompt_tokens if effective_usage else None,
            completion_tokens=effective_usage.completion_tokens if effective_usage else None,
            duration_ms=int((time.monotonic() - log.started) * 1000),
        )
        try:
            await writer(entry)
        except Exception:
            logger.warning(
                "model prompt log writer failed purpose=%s", log.purpose, exc_info=True
            )

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.close()

    def _create_kwargs(self) -> dict[str, Any]:
        """按配置附加的可选请求参数（未配置则不发送，避免端点 400）。"""
        if self._reasoning_effort is None:
            return {}
        return {"reasoning_effort": self._reasoning_effort}

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
                    **self._create_kwargs(),
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
        explicitly_unsupported = "unsupported" in code or any(
            phrase in message
            for phrase in ("not supported", "does not support", "unsupported")
        )
        response_format_referenced = param == "response_format" or "json_schema" in message
        return response_format_referenced and explicitly_unsupported

    def _is_stream_unsupported(self, exc: Exception) -> bool:
        """仅识别供应商明确返回的 stream 参数不支持，其他 400 仍正常报错。"""
        if _value(exc, "status_code") != 400:
            return False
        body = _value(exc, "body")
        error = body.get("error", body) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            return False
        param = str(error.get("param") or "").lower()
        code = str(error.get("code") or "").lower()
        message = str(error.get("message") or exc).lower()
        explicitly_unsupported = "unsupported" in code or any(
            phrase in message
            for phrase in ("not supported", "does not support", "unsupported")
        )
        if not explicitly_unsupported:
            return False
        if param == "stream":
            return True
        stream_term = r"(?:stream|streaming|stream_options)"
        return bool(
            re.search(
                rf"\b{stream_term}\b(?:\s+(?:parameter|option))?\s+"
                r"(?:is\s+)?(?:not\s+supported|unsupported)\b",
                message,
            )
            or re.search(
                rf"\b(?:does\s+not\s+support|unsupported)\s+(?:the\s+)?"
                rf"{stream_term}\b",
                message,
            )
        )

    def _completion_content(self, response: Any) -> str:
        choices = _value(response, "choices") or ()
        if not choices:
            return ""
        message = _value(choices[0], "message")
        content = _value(message, "content")
        return content if isinstance(content, str) else ""

    def _repair_message(self, exc: ValidationError | ValueError) -> dict[str, str]:
        safe_errors: list[dict[str, Any]] = []
        errors = (
            exc.errors(include_url=False, include_input=False)
            if isinstance(exc, ValidationError)
            else [{"type": "json_invalid", "loc": []}]
        )
        for error in errors[:_MAX_VALIDATION_ERRORS]:
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
