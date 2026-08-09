import pytest
from pydantic import ValidationError

from app.pi_gateway.contracts import (
    PiGatewayAdapterCatalogEntry,
    PiGatewayClaimResponse,
    PiGatewayInternalToolRequest,
    PiGatewaySourceEvent,
    PiGatewayTerminalRequest,
    RuntimeSecretEnvelope,
)


def test_internal_dtos_forbid_identity_injection_and_unknown_events() -> None:
    with pytest.raises(ValidationError):
        PiGatewayInternalToolRequest.model_validate(
            {"tool_name": "get_session_context", "args": {}, "run_id": "other"}
        )
    with pytest.raises(ValidationError):
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": "attempt-1:1",
                "sequence": 1,
                "event_type": "arbitrary.secret.dump",
                "payload": {},
            }
        )
    with pytest.raises(ValidationError):
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": "attempt-1:3",
                "sequence": 3,
                "event_type": "run.completed",
                "payload": {},
            }
        )

    with pytest.raises(ValidationError):
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": "attempt-1:2",
                "sequence": 2,
                "event_type": "message.delta",
                "payload": {"run_id": "forged"},
            }
        )

    with pytest.raises(ValidationError):
        PiGatewayTerminalRequest.model_validate(
            {
                "attempt_id": "attempt-1",
                "outcome": "completed",
                "payload": {"diagnostic": {"token": "forged"}},
            }
        )

    with pytest.raises(ValidationError):
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": "attempt-1:1",
                "sequence": 1,
                "event_type": "message.delta",
                "payload": {"diagnostic": {"token": "must-not-cross-boundary"}},
            }
        )


def test_catalog_and_secret_envelope_are_bounded_and_strict() -> None:
    entry = PiGatewayAdapterCatalogEntry(
        catalog_entry_id="catalog-1",
        adapter_visible_name="query_analysis_data",
        service="insight-cube-mcp",
        remote_name="query_analysis_data",
        input_schema_digest="sha256:" + "a" * 64,
    )
    assert entry.catalog_entry_id == "catalog-1"
    with pytest.raises(ValidationError):
        RuntimeSecretEnvelope.model_validate(
            {"alg": "AES-256-GCM", "nonce": "n", "ciphertext": "c", "token": "secret"}
        )
    with pytest.raises(ValidationError):
        PiGatewayClaimResponse.model_validate(
            {
                "run_id": "run-1",
                "attempt_id": "attempt-1",
                "lease_token": "lease-token-that-is-long-enough-123",
                "runtime_snapshot": {"api_key": "secret"},
                "transcript": [],
                "secret_envelope": {"alg": "AES-256-GCM", "nonce": "A" * 16, "ciphertext": "B" * 16},
                "adapter_catalog": [],
                "internal_tools": [],
            }
        )
