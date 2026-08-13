"""结构化字段级错误反馈（提交 1，Task 3）。

真实 UAT 证明「仅报 N error(s)」无法让模型自愈（钻取场景 7 次盲改失败）。
本模块把 Pydantic/契约校验失败收敛为有界的结构化 JSON：

- 每条错误 = ``{"path": RFC6901 JSON Pointer, "type", "reason", "retryable"}``；
- ``reason`` 只取 ``msg``，绝不含输入值（上游已用
  ``errors(include_context=False)`` 保证；本模块对 ``ValidationError``
  同样调用 ``errors(include_context=False)``，且从不写入 ``ctx``）；
- 序列化后总长不超过 :data:`MAX_PAYLOAD_ERROR_BYTES`（2048）：条目太多时
  只保留能容纳的前 N 条并置 ``truncated=True``；首条 reason 本身超长时
  截断该条 reason 并置 ``truncated=True``。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.agent_artifacts.validation import ArtifactPayloadInvalid

MAX_PAYLOAD_ERROR_BYTES = 2048

#: 单次回喂最多携带的错误条目数（足够模型定位与自愈，不撑爆上下文）。
_MAX_ERROR_ENTRIES = 8

#: 条目数超限 / reason 截断时的统一后缀。
_TRUNCATION_SUFFIX = "...(truncated)"


def _escape_token(token: str) -> str:
    """RFC 6901 转义：``~`` → ``~0``、``/`` → ``~1``。"""
    return token.replace("~", "~0").replace("/", "~1")


def _error_entries(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pydantic error dict（loc/type/msg）→ 结构化反馈条目。

    ``reason`` 只取 ``msg``，绝不携带 ``ctx`` 或任何输入值——错误回喂
    不得把用户/模型的提交内容回灌模型上下文（脱敏与不泄漏）。
    """
    entries: list[dict[str, Any]] = []
    for error in errors:
        loc = error.get("loc") or ()
        path = "/".join(_escape_token(str(part)) for part in loc)
        entries.append(
            {
                "path": f"/{path}" if path else "/",
                "type": error.get("type"),
                "reason": error.get("msg"),
                "retryable": True,
            }
        )
    return entries


def _make_body(
    error_code: str, entries: list[dict[str, Any]], total: int, truncated: bool
) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "errors": entries,
        "total_errors": total,
        "truncated": truncated,
    }


def _serialized_size(body: dict[str, Any]) -> int:
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _format_errors(error_code: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    """收敛错误集合为有界结构化反馈（≤ MAX_PAYLOAD_ERROR_BYTES 字节）。"""
    raw_entries = _error_entries(errors)
    total = len(raw_entries)
    truncated = total > _MAX_ERROR_ENTRIES
    entries = raw_entries[:_MAX_ERROR_ENTRIES]

    body = _make_body(error_code, entries, total, truncated)
    if _serialized_size(body) <= MAX_PAYLOAD_ERROR_BYTES:
        return body

    # 超限：从尾部逐条删除，直到序列化后可容纳（至少保留 1 条）。
    while (
        len(entries) > 1
        and _serialized_size(_make_body(error_code, entries[:-1], total, True))
        > MAX_PAYLOAD_ERROR_BYTES
    ):
        entries.pop()
        truncated = True

    body = _make_body(error_code, entries, total, truncated)
    if _serialized_size(body) <= MAX_PAYLOAD_ERROR_BYTES:
        return body

    # 首条 reason 本身超长：截断该条 reason（按 UTF-8 字节预算）。
    truncated = True
    first = dict(entries[0])
    prefix_body = _make_body(error_code, [dict(first, reason="")], total, True)
    budget = MAX_PAYLOAD_ERROR_BYTES - _serialized_size(prefix_body)
    if budget <= 0:
        first["reason"] = ""
    else:
        trimmed = first["reason"]
        while trimmed and len((trimmed + _TRUNCATION_SUFFIX).encode("utf-8")) > budget:
            trimmed = trimmed[:-1]
        first["reason"] = trimmed + _TRUNCATION_SUFFIX
    return _make_body(error_code, [first], total, True)


def format_payload_errors(exc: ArtifactPayloadInvalid) -> dict[str, Any]:
    """发布 payload 校验失败 → 有界结构化反馈。"""
    return _format_errors(exc.code or "artifact_payload_invalid", exc.errors or [])


def format_model_input_errors(exc: ValidationError) -> dict[str, Any]:
    """模型输入 DTO 校验失败 → 有界结构化反馈（与发布反馈同构）。"""
    return _format_errors("model_input_invalid", exc.errors(include_context=False))


__all__ = [
    "MAX_PAYLOAD_ERROR_BYTES",
    "format_model_input_errors",
    "format_payload_errors",
]
