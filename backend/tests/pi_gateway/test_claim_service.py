import pytest

from app.pi_gateway.service import (
    PiGatewayLeaseError,
    hash_lease_token,
    verify_lease_token,
)


def test_lease_token_is_verified_by_gateway_run_attempt_and_expiry() -> None:
    token = "lease-token-with-enough-entropy"
    digest = hash_lease_token(token)
    assert digest != token
    assert verify_lease_token(
        token,
        digest,
        gateway_id="gw-1",
        expected_gateway_id="gw-1",
        run_id="run-1",
        expected_run_id="run-1",
        attempt_id="attempt-1",
        expected_attempt_id="attempt-1",
        expires_at=2_000,
        now=1_999,
    ) is True
    for kwargs in (
        {"expected_gateway_id": "gw-other"},
        {"expected_run_id": "run-other"},
        {"expected_attempt_id": "attempt-other"},
        {"now": 2_001},
    ):
        with pytest.raises(PiGatewayLeaseError) as exc_info:
            verify_lease_token(
                token,
                digest,
                gateway_id="gw-1",
                expected_gateway_id=kwargs.get("expected_gateway_id", "gw-1"),
                run_id="run-1",
                expected_run_id=kwargs.get("expected_run_id", "run-1"),
                attempt_id="attempt-1",
                expected_attempt_id=kwargs.get("expected_attempt_id", "attempt-1"),
                expires_at=kwargs.get("expires_at", 2_000),
                now=kwargs.get("now", 1_999),
            )
        assert exc_info.value.code == "pi_gateway_lease_invalid"
