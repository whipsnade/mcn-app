"""离线进程级 UAT 的 fake OpenAI 兼容模型服务器。

只监听 loopback，按请求内容脚本化响应：
- ``model == "fake-current-model"``：current Runtime 请求，返回四动作协议的
  ``complete`` JSON（kill switch / current 路由场景只需要协议合法的即时完成）。
- 其余（Pi 子进程的 openai-completions 请求）：取消息历史中最后一条 user 文本的
  ``[scenario:<name>]`` 标记选择脚本；脚本步骤按已有 assistant 轮数推进。
- 响应按请求的 ``stream`` 标志分流（与真实 OpenAI 兼容端点行为一致）：
  ``stream=true`` 返回 SSE 流（``text/event-stream``，tool_calls 以 chunk 增量输出）；
  否则返回标准非流式 ``chat.completion`` JSON。current Runtime 的 ``complete_json``
  走非流式请求（``stream=False``），若一律回 SSE，客户端会解析出空 content。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


def proxy_tool_name(server: str, remote: str) -> str:
    """旧式 prefixed 可见名（server 前缀 + 点转下划线）。

    仅 ``step_mcp`` 旧兼容路径使用：生产 .mcp.json 已是 ``toolPrefix="none"``
    （模型可见名 = 裸 remote 名），prefixed 名在 adapter 分发面 fail-closed
    （tool_not_found → definitely_not_sent 释放，不计费），但计费扩展仍须把
    它映射回已审核身份（preflight 记录正确 internal 名）。
    """
    return f"{server.replace('-', '_')}_{remote.replace('.', '_')}"


def step_text(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text}


def step_internal(tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"kind": "internal_tool", "tool": tool, "args": args or {}}


def step_mcp(server: str, remote: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": "mcp",
        "server": server,
        "args": args or {},
        # mcp 代理工具的 tool 参数是 adapter 可见名
        "tool": proxy_tool_name(server, remote),
    }


def step_mcp_proxy(
    tool: str, server: str | None = None, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """真实模型的通用 mcp 代理寻址形状：裸 remote 名，server 可选。

    真实 claim 投影的 adapter_visible_name 就是裸 remote 名（如
    ``match_best_tag``），真实模型走通用 ``mcp`` 代理工具时直接以裸名寻址、
    server 可省略（唯一映射由计费扩展从 claim bindings 推导）。这与旧
    prefixed 兼容路径（``step_mcp``）是两条不同寻址形状，都必须有进程级
    覆盖——REAL_B7_20260812T045636Z_b801c490 的 L1 失败即裸名路径缺失覆盖。
    """
    return {"kind": "mcp", "server": server, "args": args or {}, "tool": tool}


def step_hang(seconds: float) -> dict[str, Any]:
    return {"kind": "hang", "seconds": seconds}


def extract_field_pairs(text: str, field: str) -> list[str]:
    """从工具结果文本按序提取 ``"field": "value"`` 字符串值。"""
    import re

    return re.findall(rf'"{field}"\s*:\s*"([0-9a-f-]{{36}})"', text)


def tool_result_texts(messages: list[dict[str, Any]]) -> list[str]:
    """按序返回所有 tool 角色消息的正文文本。"""
    texts: list[str] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, list):
            texts.append(
                "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            )
        elif isinstance(content, str):
            texts.append(content)
    return texts


def _scenario_of(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            continue
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("[scenario:") and line.endswith("]"):
                return line[len("[scenario:") : -1]
        return None
    return None


def _assistant_rounds(messages: list[dict[str, Any]]) -> int:
    return sum(1 for message in messages if message.get("role") == "assistant")


def _step_tool_fields(step: dict[str, Any]) -> tuple[str, str]:
    """工具步骤的 (tool 名, arguments JSON)（mcp 代理工具包装成 adapter 可见形态）。"""
    tool_name = step["tool"]
    arguments = json.dumps(step["args"], ensure_ascii=False)
    if step["kind"] == "mcp":
        proxy_args: dict[str, Any] = {"tool": step["tool"], "args": step["args"]}
        # server 可选：缺省时由计费扩展按 claim bindings 唯一性推导（真实代理形状）。
        if step.get("server") is not None:
            proxy_args["server"] = step["server"]
        arguments = json.dumps(proxy_args, ensure_ascii=False)
        tool_name = "mcp"
    return tool_name, arguments


def _completion_payload(step: dict[str, Any], model: str) -> dict[str, Any]:
    """非流式 ``chat.completion`` 响应体（stream 缺省/false 的请求）。"""
    message: dict[str, Any] = {"role": "assistant"}
    finish_reason = "stop"
    if step["kind"] == "text":
        message["content"] = step["text"]
    else:
        tool_name, arguments = _step_tool_fields(step)
        call_id = f"call-{step['kind']}-{int(time.time() * 1000) % 1_000_000}"
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            }
        ]
        finish_reason = "tool_calls"
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }


def _chunks_for_step(step: dict[str, Any], model: str) -> tuple[list[dict[str, Any]], str]:
    """Return (chunks, finish_reason) for one scripted step."""
    base = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    if step["kind"] == "text":
        return [
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": step["text"]}, "finish_reason": None}
                ],
            },
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            },
        ], "stop"
    tool_name, arguments = _step_tool_fields(step)
    call_id = f"call-{step['kind']}-{int(time.time() * 1000) % 1_000_000}"
    chunks = [
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": tool_name, "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
    ]
    # 参数分两片输出，模拟真实流式增量
    mid = max(1, len(arguments) // 2)
    for part in (arguments[:mid], arguments[mid:]):
        chunks.append(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": part}}]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
    chunks.append(
        {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 18, "completion_tokens": 6, "total_tokens": 24},
        }
    )
    return chunks, "tool_calls"


class FakeModelServer:
    """有状态的脚本化 fake 模型；``requests`` 记录每个请求体供断言。"""

    def __init__(self, scripts: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.scripts = scripts or {}
        self.requests: list[dict[str, Any]] = []
        self._default = [step_text("fake model response")]

    async def chat_completions(self, request: Request) -> Response:
        body = await request.json()
        self.requests.append(body)
        import os

        trace = os.environ.get("PI_UAT_MODEL_TRACE")
        if trace:
            with open(trace, "a") as fh:
                fh.write(f"model={body.get('model')} messages={len(body.get('messages') or [])}\n")
        model = str(body.get("model") or "fake-model")
        messages = list(body.get("messages") or [])
        if model == "fake-current-model":
            # current Runtime：直接产出合法的 complete 动作 JSON
            action = json.dumps(
                {"action": "complete", "text": "current runtime 回复"}, ensure_ascii=False
            )
            return self._respond(body, {"kind": "text", "text": action}, model)
        scenario = _scenario_of(messages)
        script = self.scripts.get(scenario or "", self._default)
        index = min(_assistant_rounds(messages), len(script) - 1)
        step = script[index]
        if callable(step):
            # 动态步骤：从完整消息历史解析 evidence/draft 等动态标识。
            step = step(messages)
            if inspect.isawaitable(step):
                # async callable：允许动态步骤在 pytest 进程内直接读写 DB
                # （如 Run 中途暂停租户 License）。
                step = await step
        if step["kind"] == "hang":
            await asyncio.sleep(float(step["seconds"]))
            step = {"kind": "text", "text": "延迟后的回复"}
        return self._respond(body, step, model)

    def _respond(self, body: dict[str, Any], step: dict[str, Any], model: str) -> Response:
        """按请求的 stream 标志分流：流式回 SSE，否则回标准 chat.completion JSON。"""
        if body.get("stream"):
            chunks, _finish = _chunks_for_step(step, model)
            return self._sse(chunks)
        return JSONResponse(_completion_payload(step, model))

    def _sse(self, chunks: list[dict[str, Any]]) -> StreamingResponse:
        async def stream():
            for chunk in chunks:
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                await asyncio.sleep(0)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    def app(self) -> Starlette:
        return Starlette(
            routes=[Route("/v1/chat/completions", self.chat_completions, methods=["POST"])]
        )
