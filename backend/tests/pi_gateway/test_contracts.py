import pytest
from pydantic import ValidationError

from app.pi_gateway.contracts import (
    PiGatewayAdapterCatalogEntry,
    PiGatewayClaimResponse,
    PiGatewayInternalToolRequest,
    PiGatewaySourceEvent,
    PiGatewaySourceEventBatch,
    PiGatewayTerminalRequest,
    PiGatewayProviderFailureMetadata,
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


def test_claim_snapshot_accepts_server_owned_environment_field() -> None:
    response = PiGatewayClaimResponse.model_validate(
        {
            "run_id": "run-1",
            "attempt_id": "attempt-1",
            "lease_token": "lease-token-that-is-long-enough-123",
            "lease_expires_at": 1_800_000_000.0,
            "runtime_snapshot": {"config_version_id": "cfg-1", "environment": "test"},
            "transcript": [],
            "secret_envelope": {"alg": "AES-256-GCM", "nonce": "A" * 16, "ciphertext": "B" * 16},
            "adapter_catalog": [],
            "internal_tools": [],
        }
    )
    assert response.runtime_snapshot["environment"] == "test"


def test_usage_event_is_a_bounded_internal_projection() -> None:
    event = PiGatewaySourceEvent.model_validate(
        {
            "source_event_id": "attempt-usage:1",
            "sequence": 1,
            "event_type": "usage",
            "payload": {
                "input_tokens": 12,
                "output_tokens": 3,
                "request_id": "provider-request",
                "provider": "fake-provider",
                "model": "fake-model",
            },
        }
    )
    assert event.payload["upstream_request_id"] == "provider-request"
    assert event.payload["usage_status"] == "available"
    with pytest.raises(ValidationError):
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": "attempt-usage:2",
                "sequence": 2,
                "event_type": "usage",
                "payload": {"input_tokens": -1},
            }
        )
    with pytest.raises(ValidationError):
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": "attempt-usage:3",
                "sequence": 3,
                "event_type": "usage",
                "payload": {"input_tokens": 1, "raw_response": "secret"},
            }
        )


def test_source_event_batch_is_strict_bounded_and_contiguous() -> None:
    events = [
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": f"attempt-batch:{sequence}",
                "sequence": sequence,
                "event_type": "message.start",
                "payload": {},
            }
        )
        for sequence in (1, 2)
    ]
    assert PiGatewaySourceEventBatch(events=events).events == events
    with pytest.raises(ValidationError):
        PiGatewaySourceEvent.model_validate(
            {
                "source_event_id": "attempt-batch:1",
                "sequence": "1",
                "event_type": "message.start",
                "payload": {},
            }
        )
    with pytest.raises(ValidationError, match="pi_gateway_event_batch_attempt_mismatch"):
        PiGatewaySourceEventBatch.model_validate(
            {"events": [events[0], {**events[1].model_dump(), "source_event_id": "other:2"}]}
        )
    with pytest.raises(ValidationError, match="pi_gateway_event_batch_sequence_gap"):
        PiGatewaySourceEventBatch.model_validate(
            {"events": [events[0], {**events[1].model_dump(), "sequence": 3, "source_event_id": "attempt-batch:3"}]}
        )
    with pytest.raises(ValidationError):
        PiGatewaySourceEventBatch.model_validate(
            {
                "events": [
                    {
                        "source_event_id": f"attempt-large:{sequence}",
                        "sequence": sequence,
                        "event_type": "message.delta",
                        "payload": {"text": "x" * 16_000},
                    }
                    for sequence in range(1, 10)
                ]
            }
        )
def test_provider_failure_metadata_is_strict_and_terminal_only() -> None:
    metadata = PiGatewayProviderFailureMetadata.model_validate(
        {
            "version": "provider_failure_v1",
            "failure_class": "rate_limited",
            "http_status": 429,
            "provider_request_id": "req_safe-123",
            "error_fingerprint": "a" * 64,
            "observed_at": "2026-08-21T08:00:00.000Z",
        }
    )
    request = PiGatewayTerminalRequest.model_validate(
        {
            "attempt_id": "attempt-1",
            "outcome": "failed",
            "payload": {"code": "pi_model_provider_error"},
            "failure_metadata": metadata.model_dump(mode="json"),
        }
    )
    assert request.failure_metadata == metadata
    arbitrary_safe_id = PiGatewayProviderFailureMetadata.model_validate(
        {
            "version": "provider_failure_v1",
            "failure_class": "unknown",
            "provider_request_id": "provider-req-123",
            "error_fingerprint": "b" * 64,
        }
    )
    assert arbitrary_safe_id.provider_request_id == "provider-req-123"
    with pytest.raises(ValidationError):
        PiGatewayProviderFailureMetadata.model_validate(
            {**metadata.model_dump(), "extra": "tampered"}
        )
    with pytest.raises(ValidationError):
        PiGatewayProviderFailureMetadata.model_validate(
            {**metadata.model_dump(), "http_status": 99}
        )
    with pytest.raises(ValidationError):
        PiGatewayProviderFailureMetadata.model_validate(
            {**metadata.model_dump(), "provider_request_id": "Bearer secret"}
        )
    with pytest.raises(ValidationError):
        PiGatewayProviderFailureMetadata.model_validate(
            {**metadata.model_dump(), "observed_at": "2026-02-30T08:00:00.000Z"}
        )
    with pytest.raises(ValidationError):
        PiGatewayProviderFailureMetadata.model_validate(
            {**metadata.model_dump(), "http_status": None}
        )
    with pytest.raises(ValidationError):
        PiGatewayTerminalRequest.model_validate(
            {
                "attempt_id": "attempt-1",
                "outcome": "completed",
                "payload": {},
                "failure_metadata": metadata.model_dump(mode="json"),
            }
        )
    with pytest.raises(ValidationError):
        PiGatewayTerminalRequest.model_validate(
            {
                "attempt_id": "attempt-1",
                "outcome": "failed",
                "payload": {"failure_metadata": {"errorMessage": "api_key=secret"}},
            }
        )
    with pytest.raises(ValidationError):
        PiGatewayTerminalRequest.model_validate(
            {
                "attempt_id": "attempt-1",
                "outcome": "failed",
                "payload": {"code": "worker_error"},
                "failure_metadata": metadata.model_dump(mode="json"),
            }
        )
