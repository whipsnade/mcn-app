import pytest

from app.pi_gateway.scheduler import GatewayModeError, PiRunScheduler


def test_draining_gateway_stops_new_claims_but_keeps_existing_workers() -> None:
    assert PiRunScheduler.claims_allowed(mode="active", active_runs=1, capacity=2)
    assert not PiRunScheduler.claims_allowed(mode="draining", active_runs=1, capacity=2)
    assert not PiRunScheduler.claims_allowed(mode="active", active_runs=2, capacity=2)


def test_gateway_mode_is_strict() -> None:
    with pytest.raises(GatewayModeError, match="pi_gateway_mode_invalid"):
        PiRunScheduler.validate_mode("stopped")
