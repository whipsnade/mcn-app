import pytest
from pydantic import ValidationError

from app.pi_gateway.contracts import PiGatewayClaimResponse


def _entries(count: int) -> list[dict[str, str]]:
    services = ("insight-cube-mcp", "social-grow-mcp", "bilibili-mcp")
    return [
        {
            "catalog_entry_id": f"catalog-{index}",
            "adapter_visible_name": f"adapter_{index}",
            "service": services[index % len(services)],
            "remote_name": f"remote_{index}",
            "input_schema_digest": "sha256:" + f"{index:064x}"[-64:],
        }
        for index in range(count)
    ]


def _claim(adapter_catalog: list[dict[str, str]]) -> dict:
    return {
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "lease_token": "lease-token-that-is-long-enough-1234567890",
        "lease_expires_at": 1_800_000_000.0,
        "runtime_snapshot": {"config_version_id": "cfg-1"},
        "transcript": [],
        "secret_envelope": {
            "alg": "AES-256-GCM",
            "nonce": "A" * 16,
            "ciphertext": "B" * 16,
        },
        "adapter_catalog": adapter_catalog,
        "internal_tools": [],
    }


@pytest.mark.parametrize("count", (58, 128))
def test_claim_accepts_complete_bounded_adapter_catalog(count: int) -> None:
    response = PiGatewayClaimResponse.model_validate(_claim(_entries(count)))
    assert len(response.adapter_catalog) == count
    assert response.adapter_catalog[0].adapter_visible_name == "adapter_0"
    assert response.adapter_catalog[-1].adapter_visible_name == f"adapter_{count - 1}"


def test_claim_rejects_adapter_catalog_above_bounded_capacity() -> None:
    with pytest.raises(ValidationError, match="too_large"):
        PiGatewayClaimResponse.model_validate(_claim(_entries(129)))


def test_claim_rejects_canonical_adapter_catalog_over_128_kib_before_field_limits() -> None:
    oversized = _entries(1)
    oversized[0]["remote_name"] = "x" * (128 * 1024)

    with pytest.raises(ValidationError, match="pi_gateway_claim_catalog_too_large"):
        PiGatewayClaimResponse.model_validate(_claim(oversized))


def test_claim_rejects_duplicate_normalized_adapter_identity() -> None:
    entries = _entries(2)
    entries[1]["adapter_visible_name"] = entries[0]["adapter_visible_name"]
    entries[1]["service"] = "bilibili-mcp"
    entries[0]["service"] = "aktools-mcp"

    with pytest.raises(ValidationError, match="pi_gateway_adapter_catalog_duplicate"):
        PiGatewayClaimResponse.model_validate(_claim(entries))
