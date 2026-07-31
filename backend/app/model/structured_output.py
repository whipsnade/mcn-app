from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeVar

from json_repair import repair_json
from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_TAGS = (_THINK_OPEN, _THINK_CLOSE)


@dataclass(frozen=True)
class ParsedStructuredOutput:
    raw_text: str
    json_text: str
    thinking_text: str


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return text

    first_line_end = stripped.find("\n")
    if first_line_end < 0:
        return text
    return stripped[first_line_end + 1 : -3]


def extract_single_json_object(text: str) -> str:
    source = _strip_json_fence(text).strip()
    start = source.find("{")
    if start < 0:
        raise ValueError("structured_json_invalid")

    depth = 0
    in_string = False
    escaped = False
    end: int | None = None
    for index, char in enumerate(source[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise ValueError("structured_json_invalid")
    if "{" in source[end:].strip():
        raise ValueError("structured_json_invalid")
    return source[start:end]


class ThinkJsonStreamParser:
    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._raw_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._in_think = False
        self._pending = ""

    def feed_content(self, text: str) -> tuple[str, ...]:
        self._raw_parts.append(text)
        thinking_delta: list[str] = []
        self._pending += text

        while self._pending:
            if self._pending.startswith(_THINK_OPEN):
                self._in_think = True
                self._pending = self._pending[len(_THINK_OPEN) :]
                continue
            if self._pending.startswith(_THINK_CLOSE):
                self._in_think = False
                self._pending = self._pending[len(_THINK_CLOSE) :]
                continue
            if any(tag.startswith(self._pending) for tag in _THINK_TAGS):
                break
            self._append_character(self._pending[0], thinking_delta)
            self._pending = self._pending[1:]

        return ("".join(thinking_delta),) if thinking_delta else ()

    def feed_reasoning(self, text: str) -> tuple[str, ...]:
        if not text:
            return ()
        self._thinking_parts.append(text)
        return (text,)

    def finish(self) -> ParsedStructuredOutput:
        if self._pending:
            self._append_remaining_pending()
        raw_text = "".join(self._raw_parts)
        return ParsedStructuredOutput(
            raw_text=raw_text,
            json_text=extract_single_json_object("".join(self._content_parts)),
            thinking_text="".join(self._thinking_parts),
        )

    def _append_character(self, char: str, thinking_delta: list[str]) -> None:
        if self._in_think:
            self._thinking_parts.append(char)
            thinking_delta.append(char)
        else:
            self._content_parts.append(char)

    def _append_remaining_pending(self) -> None:
        if self._in_think:
            self._thinking_parts.append(self._pending)
        else:
            self._content_parts.append(self._pending)
        self._pending = ""


def validate_with_repair(model: type[ModelT], json_text: str) -> ModelT:
    """先按严格 JSON 校验；失败时尝试 json-repair 修复未转义引号等常见问题再校验。

    修复仍失败时抛回原始校验错误，保持上层修复重试回喂的内容不变。
    """
    try:
        return model.model_validate_json(json_text, strict=True)
    except (ValidationError, ValueError) as strict_error:
        repaired = repair_json(json_text, return_objects=True)
        try:
            return model.model_validate_json(json.dumps(repaired, ensure_ascii=False), strict=True)
        except (ValidationError, ValueError):
            raise strict_error from None


def parse_non_stream_output(text: str) -> ParsedStructuredOutput:
    parser = ThinkJsonStreamParser()
    parser.feed_content(text)
    return parser.finish()
