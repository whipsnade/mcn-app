"""Terminal ordering gate: a completed outcome must never precede the assistant completion."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentEvent, AgentMessage, AgentRun
from app.pi_gateway.auth import build_signature
from app.pi_gateway.service import hash_lease_token
from app.pi_gateway.service import PiGatewayService

from .test_model_usage import _run


GATEWAY_SECRET = "test-only-gateway-secret-0123456789"
GATEWAY_ID = "gw-terminal-test"


@pytest.fixture
def gateway_settings(monkeypatch: pytest.MonkeyPatch):
    """get_settings 是 lru_cache 的：改环境后必须清缓存并在用例后还原。"""
    from app.core.config import get_settings

    monkeypatch.setenv("PI_GATEWAY_INTERNAL_SECRET", GATEWAY_SECRET)
    monkeypatch.setenv("PI_GATEWAY_ALLOWED_IDS", '["' + GATEWAY_ID + '"]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _signed_headers(body: bytes, path: str, *, nonce: str, timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "Content-Type": "application/json",
        "X-Pi-Gateway-Id": GATEWAY_ID,
        "X-Pi-Timestamp": str(ts),
        "X-Pi-Nonce": nonce,
        "X-Pi-Signature": build_signature(GATEWAY_SECRET, "POST", path, ts, nonce, body),
    }


def _arm_lease(run: AgentRun, token: str) -> None:
    expires = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=300)
    run.gateway_id = GATEWAY_ID
    run.gateway_lease_hash = hash_lease_token(token)
    run.gateway_lease_expires_at = expires
    # settle_terminal 的 worker 归属校验读通用租约字段。
    run.lease_owner = GATEWAY_ID
    run.lease_expires_at = expires


@pytest.mark.asyncio
async def test_terminal_completed_requires_persisted_assistant_completion(
    db_session, user_factory, client, gateway_settings
) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    token = "lease-token-terminal-gate-with-entropy"
    _arm_lease(run, token)
    await db_session.commit()

    path = f"/api/v1/internal/pi-gateway/v1/runs/{run.id}/terminal"
    body = f'{{"attempt_id":"{attempt.id}","outcome":"completed","payload":{{}}}}'.encode()
    response = await client.post(
        path,
        content=body,
        headers={**_signed_headers(body, path, nonce="nonce-terminal-1"), "X-Pi-Run-Lease": token},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "pi_gateway_terminal_missing_completion"
    await db_session.refresh(run)
    assert run.status == "running"

    # A failed outcome never requires an assistant completion: the run closes safely.
    fail_body = f'{{"attempt_id":"{attempt.id}","outcome":"failed","payload":{{"code":"pi_gateway_worker_failed"}}}}'.encode()
    failed = await client.post(
        path,
        content=fail_body,
        headers={**_signed_headers(fail_body, path, nonce="nonce-terminal-2"), "X-Pi-Run-Lease": token},
    )
    assert failed.status_code == 200
    await db_session.refresh(run)
    assert run.status == "failed"
    terminal_events = list(
        (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id, AgentEvent.event_type.like("run.%"))
            )
        ).all()
    )
    assert [event.event_type for event in terminal_events] == ["run.failed"]


@pytest.mark.asyncio
async def test_terminal_completed_after_message_completion_orders_events(
    db_session, user_factory, client, gateway_settings
) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    run.profile_name = "utility_v1"
    token = "lease-token-terminal-ok-with-entropy"
    _arm_lease(run, token)
    await db_session.commit()

    events_path = f"/api/v1/internal/pi-gateway/v1/runs/{run.id}/events"
    event_body = (
        f'{{"source_event_id":"{attempt.id}:1","sequence":1,'
        f'"event_type":"message.completed","payload":{{"text":"最终结论"}}}}'
    ).encode()
    event_response = await client.post(
        events_path,
        content=event_body,
        headers={**_signed_headers(event_body, events_path, nonce="nonce-terminal-3"), "X-Pi-Run-Lease": token},
    )
    assert event_response.status_code == 200

    terminal_path = f"/api/v1/internal/pi-gateway/v1/runs/{run.id}/terminal"
    terminal_body = f'{{"attempt_id":"{attempt.id}","outcome":"completed","payload":{{}}}}'.encode()
    terminal_response = await client.post(
        terminal_path,
        content=terminal_body,
        headers={
            **_signed_headers(terminal_body, terminal_path, nonce="nonce-terminal-4"),
            "X-Pi-Run-Lease": token,
        },
    )
    assert terminal_response.status_code == 200

    events = list(
        (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)
            )
        ).all()
    )
    types = [event.event_type for event in events]
    assert types == ["message.completed", "run.completed"]
    messages = list(
        (
            await db_session.scalars(
                select(AgentMessage).where(AgentMessage.run_id == run.id, AgentMessage.role == "assistant")
            )
        ).all()
    )
    assert len(messages) == 1
    assert messages[0].content == "最终结论"


@pytest.mark.asyncio
async def test_terminal_completed_after_cancel_request_settles_cancelled(
    db_session, user_factory, client, gateway_settings
) -> None:
    """取消标记先提交后，迟到的 completed ACK 不得越过取消收口。"""
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    run.profile_name = "utility_v1"
    token = "lease-token-terminal-cancel-race-with-entropy"
    _arm_lease(run, token)
    await db_session.commit()

    events_path = f"/api/v1/internal/pi-gateway/v1/runs/{run.id}/events"
    event_body = (
        f'{{"source_event_id":"{attempt.id}:1","sequence":1,'
        f'"event_type":"message.completed","payload":{{"text":"迟到结果"}}}}'
    ).encode()
    event_response = await client.post(
        events_path,
        content=event_body,
        headers={
            **_signed_headers(event_body, events_path, nonce="nonce-terminal-cancel-race-1"),
            "X-Pi-Run-Lease": token,
        },
    )
    assert event_response.status_code == 200

    run.cancel_requested = True
    await db_session.commit()

    terminal_path = f"/api/v1/internal/pi-gateway/v1/runs/{run.id}/terminal"
    terminal_body = f'{{"attempt_id":"{attempt.id}","outcome":"completed","payload":{{}}}}'.encode()
    terminal_response = await client.post(
        terminal_path,
        content=terminal_body,
        headers={
            **_signed_headers(terminal_body, terminal_path, nonce="nonce-terminal-cancel-race-2"),
            "X-Pi-Run-Lease": token,
        },
    )

    assert terminal_response.status_code == 200
    await db_session.refresh(run)
    assert run.status == "cancelled"
    terminal_events = list(
        (
            await db_session.scalars(
                select(AgentEvent).where(
                    AgentEvent.run_id == run.id,
                    AgentEvent.event_type.like("run.%"),
                )
            )
        ).all()
    )
    assert [event.event_type for event in terminal_events] == ["run.cancelled"]


@pytest.mark.asyncio
async def test_terminal_completed_formal_run_requires_a_main_report(
    db_session, user_factory, client, gateway_settings
) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    run.profile_name = "session_analyst_v1"
    token = "lease-token-terminal-main-report-with-entropy"
    _arm_lease(run, token)
    await db_session.commit()

    events_path = f"/api/v1/internal/pi-gateway/v1/runs/{run.id}/events"
    event_body = (
        f'{{"source_event_id":"{attempt.id}:1","sequence":1,'
        f'"event_type":"message.completed","payload":{{"text":"文本结论"}}}}'
    ).encode()
    event_response = await client.post(
        events_path,
        content=event_body,
        headers={
            **_signed_headers(event_body, events_path, nonce="nonce-terminal-main-report-1"),
            "X-Pi-Run-Lease": token,
        },
    )
    assert event_response.status_code == 200

    terminal_path = f"/api/v1/internal/pi-gateway/v1/runs/{run.id}/terminal"
    terminal_body = f'{{"attempt_id":"{attempt.id}","outcome":"completed","payload":{{}}}}'.encode()
    terminal_response = await client.post(
        terminal_path,
        content=terminal_body,
        headers={
            **_signed_headers(terminal_body, terminal_path, nonce="nonce-terminal-main-report-2"),
            "X-Pi-Run-Lease": token,
        },
    )

    assert terminal_response.status_code == 409
    assert terminal_response.json()["detail"] == "pi_gateway_main_artifact_missing"
    await db_session.refresh(run)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_has_assistant_completion_service_contract(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    run.profile_name = "utility_v1"
    service = PiGatewayService(db_session, gateway_id=GATEWAY_ID)
    assert await service.has_assistant_completion(run) is False
    await service.ingest_source_event(
        run,
        attempt_id=attempt.id,
        source_event_id=f"{attempt.id}:1",
        sequence=1,
        event_type="message.completed",
        payload={"text": "结论"},
    )
    assert await service.has_assistant_completion(run) is True
