from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
from app.runtime_config.crypto import SecretCipher
from app.runtime_config.schemas import RuntimeSecretBundle
from app.runtime_config.service import RuntimeConfigError, RuntimeConfigService
from app.tenancy.models import Tenant


def _cipher() -> SecretCipher:
    return SecretCipher(master_keys={"v1": b"t" * 32}, active_key_version="v1")


def _bundle() -> RuntimeSecretBundle:
    return RuntimeSecretBundle(
        model_base_url=SecretStr("https://model.example.test/v1"),
        model_api_key=SecretStr("model-secret"),
        datatap_token=SecretStr("datatap-secret"),
        datatap_urls={"social": SecretStr("https://datatap.example.test/social")},
    )


def _catalog_rows(
    count: int,
    *,
    prefix: str = "capacity",
    review_status: str = "approved",
    is_enabled: bool = True,
) -> list[McpToolCatalog]:
    now = datetime.now(UTC).replace(tzinfo=None)
    services = (
        "insight-cube-mcp",
        "social-grow-mcp",
        "social-grow-content-mcp",
        "bilibili-mcp",
    )
    return [
        McpToolCatalog(
            id=str(uuid4()),
            service_slug=services[index % len(services)],
            internal_tool_name=f"{prefix}_{index:03d}",
            reviewed_description="capacity test",
            input_schema_json={"type": "object"},
            output_validator_version="v1",
            discovery_digest=f"{index:064x}",
            review_status=review_status,
            is_enabled=is_enabled,
            created_at=now,
            updated_at=now,
        )
        for index in range(count)
    ]


async def _add_catalog(db_session, rows: list[McpToolCatalog]) -> None:
    db_session.add_all(rows)
    await db_session.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "should_fail"),
    ((32, False), (33, False), (58, False), (128, False), (129, True)),
)
async def test_reviewed_adapter_catalog_has_bounded_complete_capacity(
    db_session, count: int, should_fail: bool
) -> None:
    rows = _catalog_rows(count)
    await _add_catalog(db_session, rows)

    service = RuntimeConfigService(db_session, cipher=_cipher())
    if should_fail:
        with pytest.raises(RuntimeConfigError, match="runtime_adapter_catalog_too_large"):
            await service._reviewed_adapter_catalog()
        return

    entries = await service._reviewed_adapter_catalog()
    assert len(entries) == count
    assert [entry["adapter_visible_name"] for entry in entries] == sorted(
        entry["adapter_visible_name"] for entry in entries
    )
    assert {entry["adapter_visible_name"] for entry in entries} == {
        row.internal_tool_name for row in rows
    }


@pytest.mark.asyncio
async def test_reviewed_adapter_catalog_excludes_disabled_and_quarantined_rows(db_session) -> None:
    approved = _catalog_rows(58)
    disabled = _catalog_rows(1, prefix="disabled", is_enabled=False)
    quarantined = _catalog_rows(1, prefix="query_user_info", review_status="quarantined")
    await _add_catalog(db_session, [*approved, *disabled, *quarantined])

    entries = await RuntimeConfigService(db_session, cipher=_cipher())._reviewed_adapter_catalog()

    names = {entry["adapter_visible_name"] for entry in entries}
    assert len(entries) == 58
    assert "disabled_000" not in names
    assert "query_user_info_000" not in names


@pytest.mark.asyncio
async def test_reviewed_adapter_catalog_rejects_canonical_json_over_128_kib() -> None:
    row = SimpleNamespace(
        id="catalog-oversized",
        internal_tool_name="x" * (128 * 1024),
        service_slug="insight-cube-mcp",
        discovery_digest="a" * 64,
    )

    class Result:
        def all(self):
            return [(row, None)]

    class Database:
        async def execute(self, _statement):
            return Result()

    service = RuntimeConfigService(Database(), cipher=_cipher())
    with pytest.raises(RuntimeConfigError, match="runtime_adapter_catalog_too_large"):
        await service._reviewed_adapter_catalog()


@pytest.mark.asyncio
async def test_reviewed_adapter_catalog_rejects_ambiguous_discovery_remote_name(db_session) -> None:
    catalog = _catalog_rows(1, prefix="ambiguous")[0]
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(catalog)
    db_session.add_all(
        [
            McpToolDiscovery(
                id=str(uuid4()),
                service_slug=catalog.service_slug,
                remote_name="remote-one",
                description="first",
                input_schema_json={"type": "object"},
                output_schema_json=None,
                discovery_digest=catalog.discovery_digest,
                review_status="approved",
                discovered_at=now,
                updated_at=now,
            ),
            McpToolDiscovery(
                id=str(uuid4()),
                service_slug=catalog.service_slug,
                remote_name="remote-two",
                description="second",
                input_schema_json={"type": "object"},
                output_schema_json=None,
                discovery_digest=catalog.discovery_digest,
                review_status="approved",
                discovered_at=now,
                updated_at=now,
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(RuntimeConfigError, match="runtime_adapter_catalog_ambiguous_remote"):
        await RuntimeConfigService(db_session, cipher=_cipher())._reviewed_adapter_catalog()


@pytest.mark.asyncio
async def test_new_snapshot_keeps_complete_catalog_after_database_changes(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    rows = _catalog_rows(58, prefix="snapshot")
    await _add_catalog(db_session, rows)
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
    tenant.runtime_backend = "pi"
    await db_session.flush()
    await service.activate(version.id)

    snapshot = await service.snapshot_for_new_run(tenant.id, profile_name="session_analyst_v1")
    before = snapshot.adapter_catalog

    rows[0].internal_tool_name = "snapshot_changed_after_creation"
    await db_session.flush()

    assert len(before) == 58
    assert snapshot.adapter_catalog == before
    assert snapshot.adapter_catalog[0]["adapter_visible_name"] == "snapshot_000"
