"""0043 租户账本 downgrade guard 的真实 MySQL 验证（仅限隔离测试库）。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.command import downgrade as alembic_downgrade
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db.session import engine


def _alembic_config() -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    # 进程内执行迁移不得重置 logging（alembic.ini 的 fileConfig 会让后续用例
    # 的 caplog 失效）；这里只禁用 ini 的日志装配，迁移路径不受影响。
    config.config_file_name = None
    return config


def test_billing_guard_is_current_migration_head() -> None:
    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    assert heads == ["0043_billing_downgrade_guard"]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module", autouse=True)
def _clean_tenant_billing_residue():
    """与 test_phase2_migrations 同一干净窗口：清除已提交集成测试遗留的
    B4 租户账本残留（welcome grant/懒置备），否则首个干净窗口 downgrade
    就会被 guard 阻断并把库停在中段版本。"""

    async def _clean() -> None:
        async with engine.begin() as connection:
            exists = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tenant_wallets'"
                )
            )
            if not exists:
                return
            await connection.execute(
                text(
                    "DELETE t FROM tenant_wallet_transactions t "
                    "LEFT JOIN wallet_transactions w ON w.id = t.id WHERE w.id IS NULL"
                )
            )
            await connection.execute(text("DELETE FROM runtime_usage_records"))
            await connection.execute(
                text("UPDATE tenant_user_quota_usage SET spent = 0, reserved = 0")
            )
            await connection.execute(
                text(
                    "DELETE w FROM tenant_wallets w "
                    "LEFT JOIN tenant_billing_migration_validations v "
                    "ON v.tenant_id = w.tenant_id WHERE v.id IS NULL"
                )
            )
            await connection.execute(
                text(
                    "DELETE t FROM wallet_transactions t "
                    "LEFT JOIN tenant_memberships m ON m.user_id = t.user_id "
                    "WHERE m.user_id IS NULL"
                )
            )
            await connection.execute(
                text(
                    "DELETE w FROM wallets w "
                    "LEFT JOIN tenant_memberships m ON m.user_id = w.user_id "
                    "WHERE m.user_id IS NULL"
                )
            )

    _run(_clean())
    yield


def test_downgrade_guard_blocks_post_migration_ledger_activity() -> None:
    """新流水/用量/余额漂移存在时 downgrade 必须 fail-closed；清理后可放行。"""
    assert os.environ.get("MYSQL_DATABASE") == "kol_insight_test"
    config = _alembic_config()
    marker_id = str(uuid4())

    async def _scalar(sql: str, params: dict | None = None):
        async with engine.begin() as connection:
            return await connection.scalar(text(sql), params or {})

    async def _execute(sql: str, params: dict | None = None):
        async with engine.begin() as connection:
            await connection.execute(text(sql), params or {})

    tenant_id = _run(_scalar("SELECT tenant_id FROM tenant_wallets LIMIT 1"))
    if tenant_id is None:
        # 干净重建的测试库没有 0040 迁移的钱包行：自建最小租户+钱包作探针宿主。
        tenant_id = str(uuid4())
        _run(
            _execute(
                "INSERT INTO tenants (id, slug, name, status, is_internal, runtime_backend,"
                " license_status, active_license_id, created_at, updated_at) VALUES"
                " (:id, :slug, 'guard-probe', 'active', 0, 'current', 'active', NULL,"
                " '2026-08-10 00:00:00', '2026-08-10 00:00:00')",
                {"id": tenant_id, "slug": f"legacy-guard-probe-{tenant_id[:8]}"},
            )
        )
        _run(
            _execute(
                "INSERT INTO tenant_wallets (tenant_id, balance, reserved, version, updated_at)"
                " VALUES (:t, 0, 0, 0, '2026-08-10 00:00:00')",
                {"t": tenant_id},
            )
        )
    else:
        tenant_id = str(tenant_id)

    def _current_version() -> str | None:
        return _run(_scalar("SELECT version_num FROM alembic_version"))

    # 干净窗口：0043 -> 0042 放行（0040 迁移副本不构成新活动），随后升回 head。
    # 全程在 try/finally 内执行：任何一步失败都必须把库升回 head，
    # 否则后续测试会在中段版本上级联失败。
    try:
        alembic_downgrade(config, "0042_admin_idempotency_records")
        assert _current_version() == "0042_admin_idempotency_records"
        alembic_upgrade(config, "head")
        assert _current_version() == "0043_billing_downgrade_guard"

        # 植入一行 0040 之后的新流水（旧表无同 id 对应行）：downgrade 必须阻断。
        _run(
            _execute(
                "INSERT INTO tenant_wallet_transactions "
                "(id,tenant_id,user_id,run_id,tool_call_id,internal_tool_name,kind,"
                "balance_delta,reserved_delta,balance_after,reserved_after,"
                "idempotency_key,reference_type,reference_id,created_at) "
                "VALUES (:id,:tenant_id,NULL,NULL,NULL,NULL,'reserve',-10,10,0,10,"
                ":idem,'mcp_call',NULL,'2026-08-10 00:00:00')",
                {"id": marker_id, "tenant_id": tenant_id, "idem": f"guard-probe:{marker_id}"},
            )
        )
        with pytest.raises(Exception, match="tenant_billing_downgrade_blocked"):
            alembic_downgrade(config, "0042_admin_idempotency_records")
        assert _current_version() == "0043_billing_downgrade_guard"
    finally:
        try:
            _run(
                _execute(
                    "DELETE FROM tenant_wallet_transactions WHERE id = :id",
                    {"id": marker_id},
                )
            )
        finally:
            alembic_upgrade(config, "head")

    # 清理后同一 downgrade 再次放行。
    alembic_downgrade(config, "0042_admin_idempotency_records")
    assert _current_version() == "0042_admin_idempotency_records"
    alembic_upgrade(config, "head")
    assert _current_version() == "0043_billing_downgrade_guard"


def test_staged_downgrade_cannot_bypass_the_guard() -> None:
    """分段降级不得绕过 guard：先降到 0042（guard 通过），植入新流水后继续
    降穿 0040 时必须 fail-closed——guard 不能只挂在 0043→0042 一步上。"""
    assert os.environ.get("MYSQL_DATABASE") == "kol_insight_test"
    config = _alembic_config()
    marker_id = str(uuid4())

    async def _scalar(sql: str, params: dict | None = None):
        async with engine.begin() as connection:
            return await connection.scalar(text(sql), params or {})

    async def _execute(sql: str, params: dict | None = None):
        async with engine.begin() as connection:
            await connection.execute(text(sql), params or {})

    def _current_version() -> str | None:
        return _run(_scalar("SELECT version_num FROM alembic_version"))

    tenant_id = _run(_scalar("SELECT tenant_id FROM tenant_wallets LIMIT 1"))
    if tenant_id is None:
        tenant_id = str(uuid4())
        _run(
            _execute(
                "INSERT INTO tenants (id, slug, name, status, is_internal, runtime_backend,"
                " license_status, active_license_id, created_at, updated_at) VALUES"
                " (:id, :slug, 'staged-guard-probe', 'active', 0, 'current', 'active', NULL,"
                " '2026-08-10 00:00:00', '2026-08-10 00:00:00')",
                {"id": tenant_id, "slug": f"legacy-staged-probe-{tenant_id[:8]}"},
            )
        )
        _run(
            _execute(
                "INSERT INTO tenant_wallets (tenant_id, balance, reserved, version, updated_at)"
                " VALUES (:t, 0, 0, 0, '2026-08-10 00:00:00')",
                {"t": tenant_id},
            )
        )

    try:
        # 第一段：head → 0042（guard 运行并通过）
        alembic_downgrade(config, "0042_admin_idempotency_records")
        assert _current_version() == "0042_admin_idempotency_records"
        # 在 0043 guard 身后植入 0040 之后的新流水
        _run(
            _execute(
                "INSERT INTO tenant_wallet_transactions "
                "(id,tenant_id,user_id,run_id,tool_call_id,internal_tool_name,kind,"
                "balance_delta,reserved_delta,balance_after,reserved_after,"
                "idempotency_key,reference_type,reference_id,created_at) "
                "VALUES (:id,:tenant_id,NULL,NULL,NULL,NULL,'reserve',-10,10,0,10,"
                ":idem,'mcp_call',NULL,'2026-08-10 00:00:00')",
                {"id": marker_id, "tenant_id": tenant_id, "idem": f"staged-probe:{marker_id}"},
            )
        )
        # 第二段：继续降穿 0040——必须被拒绝（staged bypass 封堵）
        with pytest.raises(Exception, match="tenant_billing_downgrade_blocked"):
            alembic_downgrade(config, "0039a_pi_session_mutex_backfill")
    finally:
        # 清理探针并恢复 head（若第二段已被阻断，只需删行后升回）
        try:
            _run(
                _execute(
                    "DELETE FROM tenant_wallet_transactions WHERE id = :id",
                    {"id": marker_id},
                )
            )
        finally:
            alembic_upgrade(config, "head")
            assert _current_version() == "0043_billing_downgrade_guard"
