from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.agent_runtime.models import AgentRun
from app.marketing_capability_pack.runtime import MarketingRunCapability, build_marketing_run_capability
from app.marketing_skills.snapshot import SkillSnapshotService
from app.marketing_skills.validation import canonical_skill_digest
from app.runtime_config.models import RuntimeConfigVersion
from app.runtime_config.service import RuntimeConfigService


def _dynamic_capability():
    base = build_marketing_run_capability()
    content = """---
name: campaign-research
description: 数据分析 Skill
required_tools: []
---

只使用真实数据。
"""
    payload = base.model_dump(mode="json")
    payload["skills"] = [
        {
            "name": "campaign-research",
            "version": "db-revision-3",
            "revision": 3,
            "digest": canonical_skill_digest(content),
            "content": content,
            "required_tools": [],
            "artifact_contract": "analysis_report_v1",
        }
    ]
    return MarketingRunCapability.model_validate(payload)


@pytest.mark.asyncio
async def test_existing_and_child_snapshot_reuse_persisted_skill_manifest(monkeypatch) -> None:
    capability = _dynamic_capability()
    manifest = SkillSnapshotService.manifest_from_capability(capability)
    payload = {
        "config_version_id": "tenant-config",
        "runtime_contract_version": "marketing_runtime_v1",
        "runtime_backend": "pi",
        "model": {"name": "test-model", "masked_origin": "test"},
        "datatap": {"service": "test", "schema_digest": "digest"},
        "capability_pack": capability.model_dump(mode="json"),
        "skill_manifest": manifest.model_dump(mode="json"),
        "profile_name": "session_analyst_v1",
        "allowed_artifact_contracts": ["analysis_report_v1"],
        "limits": {"max_decisions": 50},
        "billing": {"mcp_call_points": 10},
    }
    config = RuntimeConfigVersion(
        id="tenant-config",
        scope="tenant",
        tenant_id="tenant-1",
        version=1,
        status="active",
        runtime_backend="pi",
        runtime_contract_version="marketing_runtime_v1",
        config_json=payload,
        secret_refs_json=[{"kind": "model_api_key"}],
        created_by="admin",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    run = AgentRun(
        id="run-1",
        session_id="session-1",
        user_id="user-1",
        tenant_id="tenant-1",
        runtime_backend="pi",
        runtime_config_version_id="tenant-config",
        runtime_config_snapshot_json=payload,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        run_kind="user",
        visibility="user",
        status="running",
    )
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[config, config, config])
    service = RuntimeConfigService(db)
    resolver = AsyncMock(side_effect=AssertionError("existing/child must not resolve active skills"))
    monkeypatch.setattr(SkillSnapshotService, "resolve_for_new_run", resolver)

    existing = await service.snapshot_for_existing_run(run)
    child = await service.snapshot_for_child_run(run, profile_name="session_analyst_v1")

    assert existing.skill_manifest.manifest_digest == manifest.manifest_digest
    assert child.skill_manifest.manifest_digest == manifest.manifest_digest
    assert child.capability_pack["skills"][0]["revision"] == 3
    resolver.assert_not_awaited()
