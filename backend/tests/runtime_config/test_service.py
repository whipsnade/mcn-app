from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentSession
from app.runtime_config.models import RuntimeConfigVersion
from app.runtime_config.crypto import SecretCipher
from app.runtime_config.schemas import RuntimeSecretBundle
from app.runtime_config.service import RuntimeConfigError, RuntimeConfigService
from app.tenancy.models import Tenant


def _bundle() -> RuntimeSecretBundle:
    return RuntimeSecretBundle(
        model_base_url=SecretStr("https://model.example.test/v1"),
        model_api_key=SecretStr("model-secret"),
        datatap_token=SecretStr("datatap-secret"),
        datatap_urls={"social": SecretStr("https://datatap.example.test/social")},
    )


def _cipher() -> SecretCipher:
    return SecretCipher(master_keys={"v1": b"t" * 32}, active_key_version="v1")


@pytest.mark.asyncio
async def test_tenant_runtime_config_snapshot_is_secret_free_and_switchable(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant_id = (await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))).id
    service = RuntimeConfigService(db_session, cipher=_cipher())
    version = await service.create_tenant_version(
        tenant_id,
        created_by=user.id,
        runtime_backend="pi",
        model={"name": "test-model", "masked_origin": "https://model.example.test"},
        datatap={"service": "social", "schema_digest": "digest-social"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
        secrets=_bundle(),
    )
    tenant = await db_session.get(Tenant, tenant_id)
    tenant.runtime_backend = "pi"
    await db_session.flush()
    await service.activate(version.id)

    snapshot = await service.snapshot_for_new_run(tenant_id, profile_name="session_analyst_v1")
    dumped = snapshot.model_dump(mode="json")
    assert snapshot.runtime_backend == "pi"
    assert snapshot.config_version_id == version.id
    assert "model-secret" not in repr(snapshot)
    assert "datatap-secret" not in str(dumped)
    assert dumped["model"]["masked_origin"] == "https://model.example.test"
    assert dumped["profile_name"] == "session_analyst_v1"
    assert dumped["required_artifact_contract"] is None
    assert dumped["artifact_contract_mode"] is None
    assert dumped["allowed_artifact_contracts"] == [
        "brand_report_v3",
        "campaign_report_v3",
        "insight_board_v1",
        "kol_selection_v3",
    ]
    assert dumped["capability_pack_version"] == dumped["capability_pack"]["pack_version"]
    assert dumped["capability_pack_manifest_digest"] == dumped["capability_pack"]["manifest_digest"]

    now = datetime.now(UTC).replace(tzinfo=None)
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        tenant_id=tenant_id,
        title="runtime child snapshot",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    parent = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        tenant_id=tenant_id,
        runtime_backend="pi",
        runtime_config_version_id=version.id,
        runtime_config_snapshot_json=dumped,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        run_kind="user",
        visibility="user",
        status="queued",
        created_at=now,
        queued_at=now,
    )
    db_session.add(parent)
    await db_session.flush()

    child_snapshot = await service.snapshot_for_child_run(
        parent, profile_name="kol_detail_v1"
    )
    assert child_snapshot.profile_name == "kol_detail_v1"
    assert child_snapshot.required_artifact_contract is None
    assert child_snapshot.allowed_artifact_contracts == ("insight_board_v1",)
    assert child_snapshot.capability_pack_manifest_digest == snapshot.capability_pack_manifest_digest
    assert child_snapshot.adapter_catalog == snapshot.adapter_catalog


@pytest.mark.asyncio
async def test_new_run_snapshot_uses_capability_allowlist_not_profile_required_contract(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    service = RuntimeConfigService(db_session, cipher=_cipher())
    version = await service.create_tenant_version(
        tenant.id,
        created_by=user.id,
        runtime_backend="pi",
        model={"name": "test-model", "masked_origin": "https://model.example.test"},
        datatap={"service": "social", "schema_digest": "digest-social"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
        secrets=_bundle(),
    )
    tenant.runtime_backend = "pi"
    await db_session.flush()
    await service.activate(version.id)

    snapshot = await service.snapshot_for_new_run(tenant.id, profile_name="session_analyst_v1")

    assert snapshot.required_artifact_contract is None
    assert snapshot.artifact_contract_mode is None
    assert snapshot.allowed_artifact_contracts == (
        "brand_report_v3",
        "campaign_report_v3",
        "insight_board_v1",
        "kol_selection_v3",
    )


@pytest.mark.asyncio
async def test_runtime_config_does_not_persist_profile_artifact_mapping(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    service = RuntimeConfigService(db_session, cipher=_cipher())

    version = await service.create_tenant_version(
        tenant.id,
        created_by=user.id,
        runtime_backend="current",
        model={"name": "test-model", "masked_origin": "test"},
        datatap={"service": "social", "schema_digest": "digest"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
    )
    assert "profile_artifact_contracts" not in version.config_json


@pytest.mark.asyncio
async def test_activation_allows_artifact_capable_profiles_without_mapping(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    service = RuntimeConfigService(db_session, cipher=_cipher())
    version = await service.create_tenant_version(
        tenant.id,
        created_by=user.id,
        runtime_backend="pi",
        model={"name": "test-model", "masked_origin": "test"},
        datatap={"service": "social", "schema_digest": "digest"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
        secrets=_bundle(),
    )

    await service.activate(version.id)
    snapshot = RuntimeConfigService._snapshot_from_config(
        version, profile_name="kol_detail_v1"
    )
    assert snapshot.required_artifact_contract is None
    assert snapshot.allowed_artifact_contracts == ("insight_board_v1",)


@pytest.mark.asyncio
async def test_runtime_config_activation_is_append_only_and_retires_previous(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    service = RuntimeConfigService(db_session, cipher=_cipher())
    first = await service.create_tenant_version(
        tenant.id,
        created_by=user.id,
        runtime_backend="current",
        model={"name": "one", "masked_origin": "https://one.example"},
        datatap={"service": "social", "schema_digest": "one"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
    )
    second = await service.create_tenant_version(
        tenant.id,
        created_by=user.id,
        runtime_backend="current",
        model={"name": "two", "masked_origin": "https://two.example"},
        datatap={"service": "social", "schema_digest": "two"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
    )
    await service.activate(first.id)
    await service.activate(second.id)

    assert (await db_session.get(RuntimeConfigVersion, first.id)).status == "retired"
    assert (await db_session.get(RuntimeConfigVersion, second.id)).status == "active"
    with pytest.raises(RuntimeConfigError, match="runtime_config_immutable"):
        await service.update_version(first.id, config_json={"tampered": True})


@pytest.mark.asyncio
async def test_legacy_current_config_cannot_resolve_secret_bundle_and_pi_requires_override(
    db_session, user_factory
) -> None:
    await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    service = RuntimeConfigService(db_session, cipher=_cipher())
    snapshot = await service.snapshot_for_new_run(tenant.id)
    assert snapshot.runtime_backend == "current"
    with pytest.raises(RuntimeConfigError, match="runtime_secret_run_mismatch"):
        await service.resolve_secret_bundle(snapshot.config_version_id, "run-1")

    tenant.runtime_backend = "pi"
    await db_session.flush()
    with pytest.raises(RuntimeConfigError, match="runtime_config_required"):
        await service.snapshot_for_new_run(tenant.id)


@pytest.mark.asyncio
async def test_runtime_contract_tampering_fails_closed(db_session, user_factory) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    service = RuntimeConfigService(db_session, cipher=_cipher())
    version = await service.create_tenant_version(
        tenant.id,
        created_by=user.id,
        runtime_backend="current",
        model={"name": "test", "masked_origin": "https://model.example"},
        datatap={"service": "social", "schema_digest": "digest"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
        runtime_contract_version="marketing_runtime_v0",
    )
    await service.activate(version.id)
    with pytest.raises(RuntimeConfigError, match="runtime_contract_unsupported"):
        await service.snapshot_for_new_run(tenant.id)


@pytest.mark.asyncio
async def test_secret_bundle_requires_the_exact_run_snapshot_and_decrypts_only_for_that_run(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    service = RuntimeConfigService(db_session, cipher=_cipher())
    version = await service.create_tenant_version(
        tenant.id,
        created_by=user.id,
        runtime_backend="pi",
        model={"name": "pi-model", "masked_origin": "https://model.example"},
        datatap={"service": "social", "schema_digest": "digest"},
        limits={"max_decisions": 10},
        billing={"mcp_call_points": 10},
        secrets=_bundle(),
    )
    tenant.runtime_backend = "pi"
    await service.activate(version.id)
    snapshot = await service.snapshot_for_new_run(tenant.id)
    now = datetime.now(UTC).replace(tzinfo=None)
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        tenant_id=tenant.id,
        title="runtime secrets",
        status="active",
        created_at=now,
        updated_at=now,
    )
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        tenant_id=tenant.id,
        runtime_backend=snapshot.runtime_backend,
        runtime_config_version_id=snapshot.config_version_id,
        runtime_config_snapshot_json=snapshot.model_dump(mode="json"),
        queued_at=now,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="pi-model",
        status="queued",
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add(run)
    await db_session.flush()

    resolved = await service.resolve_secret_bundle(version.id, run.id)
    assert resolved.model_api_key.get_secret_value() == "model-secret"
    assert resolved.datatap_token.get_secret_value() == "datatap-secret"
    with pytest.raises(RuntimeConfigError, match="runtime_secret_run_mismatch"):
        await service.resolve_secret_bundle("legacy-env-v1", run.id)
