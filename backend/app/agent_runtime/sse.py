"""SSE 编码与心跳分块（纯函数，无 DB 依赖）。

模式对齐 ``app/tasks/router.py`` 的 ``encode_sse_event`` /
``sse_event_chunks``；事件 id 使用 per-run ``sequence`` 而非全局自增，
配合客户端 ``Last-Event-ID`` 做断线续传（spec §15.3）。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from app.agent_runtime.models import AgentEvent


def encode_sse_event(
    run_id: str, sequence: int, event_type: str, payload: dict[str, Any] | None
) -> str:
    """编码单条 SSE 帧；``data`` 负载的 ``run_id`` 以服务端为准。"""
    # 服务端 run_id 放在合并右侧强制覆盖，客户端传的同名键不能伪造
    data = {**(payload or {}), "run_id": run_id}
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {body}\n\n"


async def sse_event_chunks(
    events: AsyncIterator[AgentEvent], *, heartbeat_seconds: float = 15
) -> AsyncIterator[str]:
    """把事件迭代器转成 SSE 分块；空闲超过心跳间隔产出 ``: heartbeat``。"""
    iterator = events.__aiter__()
    pending = asyncio.ensure_future(anext(iterator))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield ": heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            yield encode_sse_event(
                event.run_id, event.sequence, event.event_type, event.payload_json
            )
            pending = asyncio.ensure_future(anext(iterator))
    finally:
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
