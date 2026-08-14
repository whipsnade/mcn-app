"""DB nonce barrier window tests: expiry must cover the whole signature window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.pi_gateway.auth import build_signature
from app.pi_gateway.models import PiGatewayRequestNonce


GATEWAY_SECRET = "test-only-gateway-secret-0123456789"
GATEWAY_ID = "gw-nonce-test"
CLAIMS_PATH = "/api/v1/internal/pi-gateway/v1/claims"


@pytest.fixture
def gateway_settings(monkeypatch: pytest.MonkeyPatch):
    """get_settings 是 lru_cache 的：改环境后必须清缓存并在用例后还原。"""
    from app.core.config import get_settings

    monkeypatch.setenv("PI_GATEWAY_INTERNAL_SECRET", GATEWAY_SECRET)
    monkeypatch.setenv("PI_GATEWAY_ALLOWED_IDS", '["' + GATEWAY_ID + '"]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _signed(body: bytes, *, nonce: str, timestamp: int) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Pi-Gateway-Id": GATEWAY_ID,
        "X-Pi-Timestamp": str(timestamp),
        "X-Pi-Nonce": nonce,
        "X-Pi-Signature": build_signature(GATEWAY_SECRET, "POST", CLAIMS_PATH, timestamp, nonce, body),
    }


def _patch_clock(monkeypatch: pytest.MonkeyPatch, current: list[datetime]) -> None:
    def _now() -> datetime:
        return current[0]

    # router._utc_now 是 _authenticate 的唯一时钟入口（验签 now 由它显式传入）。
    monkeypatch.setattr("app.pi_gateway.router._utc_now", _now)


@pytest.mark.asyncio
async def test_nonce_survives_full_window_for_fast_client_clock(
    db_session, client, monkeypatch, gateway_settings
) -> None:
    """A client 29s fast stays verifiable until ts+30; a replay inside that
    window must still hit the DB nonce barrier (the old now+30 expiry dropped
    the row up to 29s early)."""
    server_now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    current = [server_now]
    _patch_clock(monkeypatch, current)

    client_ts = int((server_now + timedelta(seconds=29)).replace(tzinfo=UTC).timestamp())
    body = b'{"capacity":1}'
    first = await client.post(CLAIMS_PATH, content=body, headers=_signed(body, nonce="fast-1", timestamp=client_ts))
    assert first.status_code == 204

    current[0] = server_now + timedelta(seconds=59)  # |59-29| = 30: signature still valid
    replay = await client.post(CLAIMS_PATH, content=body, headers=_signed(body, nonce="fast-1", timestamp=client_ts))
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_nonce_replay_rejected_for_slow_client_clock(db_session, client, monkeypatch, gateway_settings) -> None:
    server_now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    current = [server_now]
    _patch_clock(monkeypatch, current)

    client_ts = int((server_now - timedelta(seconds=29)).replace(tzinfo=UTC).timestamp())
    body = b'{"capacity":1}'
    first = await client.post(CLAIMS_PATH, content=body, headers=_signed(body, nonce="slow-1", timestamp=client_ts))
    assert first.status_code == 204
    current[0] = server_now + timedelta(seconds=1)
    replay = await client.post(CLAIMS_PATH, content=body, headers=_signed(body, nonce="slow-1", timestamp=client_ts))
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_nonce_row_expiry_is_derived_from_signed_timestamp(
    db_session, client, monkeypatch, gateway_settings
) -> None:
    server_now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    current = [server_now]
    _patch_clock(monkeypatch, current)

    client_ts = int((server_now + timedelta(seconds=29)).replace(tzinfo=UTC).timestamp())
    body = b'{"capacity":1}'
    response = await client.post(CLAIMS_PATH, content=body, headers=_signed(body, nonce="edge-1", timestamp=client_ts))
    assert response.status_code == 204
    row = await db_session.scalar(
        select(PiGatewayRequestNonce).where(
            PiGatewayRequestNonce.gateway_id == GATEWAY_ID,
            PiGatewayRequestNonce.nonce == "edge-1",
        )
    )
    assert row is not None
    expected = datetime.fromtimestamp(client_ts, UTC).replace(tzinfo=None) + timedelta(seconds=31)
    assert row.expires_at >= datetime.fromtimestamp(client_ts, UTC).replace(tzinfo=None) + timedelta(seconds=30)
    assert row.expires_at <= expected


@pytest.mark.asyncio
async def test_nonce_barrier_commits_before_business_rollback(db_session, client, monkeypatch, gateway_settings) -> None:
    """A business failure after authentication must not roll back the nonce."""
    server_now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    current = [server_now]
    _patch_clock(monkeypatch, current)

    ts = int(server_now.replace(tzinfo=UTC).timestamp())
    path = "/api/v1/internal/pi-gateway/v1/runs/run-missing/terminal"
    body = b'{"attempt_id":"attempt-missing","outcome":"failed","payload":{}}'
    headers = {
        "Content-Type": "application/json",
        "X-Pi-Gateway-Id": GATEWAY_ID,
        "X-Pi-Timestamp": str(ts),
        "X-Pi-Nonce": "barrier-1",
        "X-Pi-Signature": build_signature(GATEWAY_SECRET, "POST", path, ts, "barrier-1", body),
        "X-Pi-Run-Lease": "lease-token-with-enough-entropy",
    }
    first = await client.post(path, content=body, headers=headers)
    assert first.status_code == 404  # business rejection: unknown run
    replay = await client.post(path, content=body, headers=headers)
    assert replay.status_code == 401  # the committed nonce barrier rejects the replay
