from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import engine
from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
from app.runtime_config.crypto import SecretCipher
from app.runtime_config.schemas import RuntimeSecretBundle
from app.runtime_config.service import RuntimeConfigError, RuntimeConfigService
from app.tenancy.models import Tenant


async def _reviewed_catalog_ids(session: AsyncSession) -> set[str]:
    result = await session.execute(
        select(McpToolCatalog.id).where(
            McpToolCatalog.review_status == "approved",
            McpToolCatalog.is_enabled.is_(True),
        )
    )
    return set(result.scalars().all())


@pytest_asyncio.fixture
async def isolated_reviewed_catalog(db_session) -> set[str]:
    """在本测试事务内临时禁用预存的 reviewed catalog 行。

    `kol_insight_test` 中的 lifecycle 现场行（当前 29 条 approved+enabled）是其他
    验证留下的真实事实，禁止删除；容量断言需要确定性的 reviewed 集合。这里只在
    db_session 的外层事务内把它们临时置为 disabled，事务回滚后现场原样恢复
    （由 test_precexisting_catalog_rows_restored_after_rollback 证明）。
    """
    preexisting_ids = await _reviewed_catalog_ids(db_session)
    if preexisting_ids:
        await db_session.execute(
            update(McpToolCatalog)
            .where(McpToolCatalog.id.in_(preexisting_ids))
            .values(is_enabled=False)
        )
        await db_session.flush()
    return preexisting_ids


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
    db_session, isolated_reviewed_catalog, count: int, should_fail: bool
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
async def test_reviewed_adapter_catalog_excludes_disabled_and_quarantined_rows(
    db_session, isolated_reviewed_catalog
) -> None:
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

    class ScalarResult:
        def all(self):
            return [row]

    class CatalogResult:
        def scalars(self):
            return ScalarResult()

    class DiscoveryResult:
        def all(self):
            return []

    class Database:
        def __init__(self):
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            return CatalogResult() if self.calls == 1 else DiscoveryResult()

    service = RuntimeConfigService(Database(), cipher=_cipher())
    with pytest.raises(RuntimeConfigError, match="runtime_adapter_catalog_too_large"):
        await service._reviewed_adapter_catalog()


@pytest.mark.asyncio
async def test_reviewed_adapter_catalog_rejects_ambiguous_discovery_remote_name(
    db_session, isolated_reviewed_catalog
) -> None:
    # discovery_digest = SHA256(name + input_schema + output_schema)，正常两个
    # 工具不可能仅因 Schema 相同而共享 digest；同一 (service, digest) 对应多个
    # approved remote name 只能来自异常数据库状态、重复审批绑定、数据损坏或
    # 极端 digest 冲突。这里手工构造该异常状态，锁定 fail-closed 行为。
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
    db_session, isolated_reviewed_catalog, user_factory
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


@pytest.mark.asyncio
async def test_precexisting_catalog_rows_restored_after_rollback() -> None:
    """证明隔离 fixture 的事务回滚恢复 lifecycle 现场，而不是删除现场。

    使用独立连接 + 显式回滚（不依赖 db_session teardown 顺序）：事务内把预存
    approved+enabled catalog 行临时禁用并确认可见数为 0，回滚后用新连接验证
    现场行数与禁用前完全一致。测试库没有现场行时该证明退化为空集往返。
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            preexisting_ids = await _reviewed_catalog_ids(session)
            if preexisting_ids:
                await session.execute(
                    update(McpToolCatalog)
                    .where(McpToolCatalog.id.in_(preexisting_ids))
                    .values(is_enabled=False)
                )
                await session.flush()
                visible = await session.scalar(
                    select(func.count())
                    .select_from(McpToolCatalog)
                    .where(
                        McpToolCatalog.review_status == "approved",
                        McpToolCatalog.is_enabled.is_(True),
                    )
                )
                assert visible == 0
        finally:
            await session.close()
            await transaction.rollback()

    async with engine.connect() as connection:
        restored = await connection.scalar(
            select(func.count())
            .select_from(McpToolCatalog)
            .where(
                McpToolCatalog.review_status == "approved",
                McpToolCatalog.is_enabled.is_(True),
            )
        )
    assert restored == len(preexisting_ids)
