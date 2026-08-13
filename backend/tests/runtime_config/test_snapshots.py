import json

import pytest
from pydantic import ValidationError

from app.marketing_capability_pack.runtime import MarketingRunCapability, build_marketing_run_capability
from app.runtime_config.schemas import RuntimeConfigSnapshot


def test_runtime_snapshot_has_fixed_contract_and_forbids_extra_fields() -> None:
    snapshot = RuntimeConfigSnapshot(
        config_version_id="cfg-1",
        runtime_contract_version="marketing_runtime_v1",
        runtime_backend="current",
        model={"name": "test", "masked_origin": "https://model.example"},
        datatap={"service": "social", "schema_digest": "digest"},
        capability_pack={"manifest_digest": "digest-pack"},
        limits={"max_decisions": 50},
        billing={"mcp_call_points": 10},
    )
    assert snapshot.model_dump(mode="json")["runtime_contract_version"] == "marketing_runtime_v1"
    with pytest.raises(ValidationError):
        RuntimeConfigSnapshot.model_validate({**snapshot.model_dump(), "api_key": "secret"})
    with pytest.raises(ValidationError, match="runtime_snapshot_secret_field"):
        RuntimeConfigSnapshot.model_validate(
            {
                **snapshot.model_dump(),
                "capability_pack": {"nested": {"authorization": "Bearer secret"}},
            }
    )
    for ordinary in ("token 说明文字", "endpoint 说明文字"):
        permitted = RuntimeConfigSnapshot.model_validate(
            {**snapshot.model_dump(), "datatap": {"service": ordinary, "schema_digest": "x"}}
        )
        assert permitted.datatap["service"] == ordinary
    for credential in (
        "sk-test-secret-value",
        "Bearer test-secret",
        "https://example.test/?token=test-secret",
        "mysql://user:password@example.test/db",
    ):
        with pytest.raises(ValidationError, match="runtime_snapshot_secret_field"):
            try:
                RuntimeConfigSnapshot.model_validate(
                    {**snapshot.model_dump(), "model": {"name": credential, "masked_origin": "test"}}
                )
            except ValidationError as error:
                assert credential not in str(error)
                raise


def test_runtime_snapshot_freezes_profile_contract_and_pack_audit_fields() -> None:
    capability = build_marketing_run_capability(model_version="test-model")
    snapshot = RuntimeConfigSnapshot(
        config_version_id="cfg-brand",
        runtime_contract_version="marketing_runtime_v1",
        runtime_backend="pi",
        model={"name": "test", "masked_origin": "https://model.example"},
        datatap={"service": "social", "schema_digest": "digest"},
        capability_pack=capability.model_dump(mode="json"),
        profile_name="session_analyst_v1",
        artifact_contract_mode="required",
        required_artifact_contract="brand_report_v3",
        capability_pack_version=capability.pack_version,
        capability_pack_manifest_digest=capability.manifest_digest,
        limits={"max_decisions": 50},
        billing={"mcp_call_points": 10},
        adapter_catalog=[{"remote_name": "query_analysis_data"}],
    )

    dumped = snapshot.model_dump(mode="json")
    assert dumped["required_artifact_contract"] == "brand_report_v3"
    assert dumped["capability_pack_version"] == capability.pack_version
    assert dumped["capability_pack_manifest_digest"] == capability.manifest_digest

    with pytest.raises(ValidationError, match="runtime_snapshot_capability_audit_mismatch"):
        RuntimeConfigSnapshot.model_validate(
            {
                **dumped,
                "capability_pack_manifest_digest": "f" * 64,
            }
        )

    with pytest.raises(TypeError):
        snapshot.model["name"] = "mutated"
    with pytest.raises(TypeError):
        snapshot.capability_pack["skills"] = []
    with pytest.raises(TypeError):
        snapshot.adapter_catalog[0]["remote_name"] = "mutated"


def test_persisted_capability_snapshot_revalidates_runtime_contract_and_root_digest() -> None:
    capability = build_marketing_run_capability(model_version="test-model")
    payload = capability.model_dump(mode="json")
    assert MarketingRunCapability.model_validate(payload).runtime_contract_version == "marketing_runtime_v1"

    tampered = json.loads(json.dumps(payload))
    tampered["runtime_contract_version"] = "marketing_runtime_v0"
    with pytest.raises(ValidationError, match="marketing_runtime_contract_unsupported"):
        MarketingRunCapability.model_validate(tampered)


def test_runtime_snapshot_accepts_versioned_public_price_table_only() -> None:
    base = RuntimeConfigSnapshot(
        config_version_id="cfg-price",
        runtime_contract_version="marketing_runtime_v1",
        runtime_backend="pi",
        model={"name": "test", "masked_origin": "https://model.example"},
        datatap={"service": "social", "schema_digest": "digest"},
        capability_pack={"manifest_digest": "digest-pack"},
        limits={"max_decisions": 50},
        billing={
            "mcp_call_points": 10,
            "price_table": {
                "version": "price-v1",
                "currency": "USD",
                "input_micros_per_million": 1_000_000,
            },
        },
    )
    assert isinstance(base.billing["price_table"], dict)
    with pytest.raises(ValidationError):
        RuntimeConfigSnapshot.model_validate(
            {**base.model_dump(), "billing": {"price_table": {"api_key": "secret"}}}
        )
