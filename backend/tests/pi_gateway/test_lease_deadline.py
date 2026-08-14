"""claim/heartbeat 响应必须暴露明确 lease deadline（供 Gateway fencing）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.pi_gateway.contracts import PiGatewayClaimResponse


def test_claim_response_carries_explicit_lease_deadline() -> None:
    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=60)
    response = PiGatewayClaimResponse(
        run_id="run-1",
        attempt_id="attempt-1",
        lease_token="x" * 32,
        lease_expires_at=expiry.timestamp(),
        runtime_snapshot={"config_version_id": "cfg-1"},
        secret_envelope={
            "alg": "AES-256-GCM",
            "nonce": "n" * 16,
            "ciphertext": "c" * 16,
        },
    )
    assert response.lease_expires_at == expiry.timestamp()


def test_lease_deadline_epoch_treats_naive_datetime_as_utc() -> None:
    """naive datetime 一律按 UTC 解释（.timestamp() 会按本地时区，UTC+8 偏 8h）。"""
    import calendar

    from app.pi_gateway.service import lease_deadline_epoch

    naive = datetime(2026, 8, 11, 2, 53, 38)
    assert lease_deadline_epoch(naive) == calendar.timegm(naive.timetuple())
