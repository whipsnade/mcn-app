from __future__ import annotations

from datetime import UTC, datetime

from app.marketing_capability_pack.runtime import build_marketing_run_capability
from app.runtime_config.models import RuntimeConfigVersion
from app.runtime_config.service import RuntimeConfigService


def test_snapshot_from_config_can_carry_skill_manifest_without_profile_policy() -> None:
    capability = build_marketing_run_capability()
    config = RuntimeConfigVersion(
        id="current-config",
        scope="system",
        tenant_id=None,
        version=1,
        status="active",
        runtime_backend="current",
        runtime_contract_version="marketing_runtime_v1",
        config_json={
            "config_version_id": "current-config",
            "runtime_contract_version": "marketing_runtime_v1",
            "runtime_backend": "current",
            "model": {"name": "current", "masked_origin": "test"},
            "datatap": {"service": "test", "schema_digest": "digest"},
            "capability_pack": capability.model_dump(mode="json"),
            "limits": {"max_decisions": 50},
            "billing": {"mcp_call_points": 10},
        },
        secret_refs_json=[],
        created_by=None,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    snapshot = RuntimeConfigService._snapshot_from_config(
        config,
        profile_name="session_analyst_v1",
    )

    assert snapshot.skill_manifest is None
    assert snapshot.required_artifact_contract is None
