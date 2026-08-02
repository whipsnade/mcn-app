import asyncio
import json
from types import SimpleNamespace

from app.agent_runtime.sse import encode_sse_event, sse_event_chunks


def _event(run_id, sequence, event_type, payload) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id, sequence=sequence, event_type=event_type, payload_json=payload
    )


def test_encode_sse_event_framing_and_run_id() -> None:
    encoded = encode_sse_event("run-1", 7, "thinking.delta", {"text": "hi"})

    assert encoded.startswith("id: 7\n")
    assert "event: thinking.delta\n" in encoded
    assert encoded.endswith("\n\n")
    data_lines = [line for line in encoded.split("\n") if line.startswith("data:")]
    assert len(data_lines) == 1
    data = json.loads(data_lines[0][len("data:") :])
    assert data["run_id"] == "run-1"
    assert data["text"] == "hi"


def test_encode_sse_event_escapes_newlines_in_payload() -> None:
    encoded = encode_sse_event("run-1", 2, "thinking.delta", {"text": "line1\nline2"})

    # JSON 转义后 data 仍是单行，不会产生裸换行破坏 SSE 帧
    data_lines = [line for line in encoded.split("\n") if line.startswith("data:")]
    assert len(data_lines) == 1
    data = json.loads(data_lines[0][len("data:") :])
    assert data["text"] == "line1\nline2"


async def test_sse_event_chunks_heartbeats_when_idle_and_forwards_events() -> None:
    event_1 = _event("run-1", 1, "run.started", {"run_id": "run-1"})
    event_2 = _event("run-1", 2, "run.completed", {"run_id": "run-1"})

    async def events():
        await asyncio.sleep(0.16)
        yield event_1
        yield event_2

    chunks = [chunk async for chunk in sse_event_chunks(events(), heartbeat_seconds=0.05)]

    assert chunks.count(": heartbeat\n\n") >= 1
    assert chunks[0] == ": heartbeat\n\n"
    encoded_1 = encode_sse_event(
        event_1.run_id, event_1.sequence, event_1.event_type, event_1.payload_json
    )
    encoded_2 = encode_sse_event(
        event_2.run_id, event_2.sequence, event_2.event_type, event_2.payload_json
    )
    assert encoded_1 in chunks
    assert chunks[-1] == encoded_2
    # 心跳只发生在事件到达前的空闲期，事件送达后按序透传
    assert chunks.index(encoded_1) < chunks.index(encoded_2)
