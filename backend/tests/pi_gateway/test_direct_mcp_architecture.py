from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.pi_gateway.contracts import PiGatewayMcpFinalizeRequest


def test_finalize_accepts_only_small_accounting_metadata() -> None:
    request = PiGatewayMcpFinalizeRequest(
        permit_id="permit-1",
        outcome="succeeded",
        upstream_request_id="upstream-1",
        response_bytes=42,
        adapter_version="pi-adapter-v1",
        completed_at="2026-08-13T10:00:00Z",
        response_hash="sha256:" + "a" * 64,
    )

    assert request.outcome == "succeeded"
    assert request.response_bytes == 42
    assert request.model_dump(exclude={"permit_id"}) == {
        "outcome": "succeeded",
        "upstream_request_id": "upstream-1",
        "response_bytes": 42,
        "adapter_version": "pi-adapter-v1",
        "completed_at": "2026-08-13T10:00:00Z",
        "response_hash": "sha256:" + "a" * 64,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"permit_id": "permit-1", "details": {"structuredContent": {"rows": [1]}}},
        {"permit_id": "permit-1", "outcome": "succeeded", "payload": {"rows": [1]}},
        {"permit_id": "permit-1", "outcome": "succeeded", "structuredContent": {"rows": [1]}},
    ],
)
def test_finalize_rejects_business_payload_or_legacy_envelope(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PiGatewayMcpFinalizeRequest.model_validate(payload)
