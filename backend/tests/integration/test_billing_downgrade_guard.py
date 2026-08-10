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
    assert tenant_id is not None, "测试库应已含 0040 迁移的租户钱包行"

    def _current_version() -> str | None:
        return _run(_scalar("SELECT version_num FROM alembic_version"))

    # 干净窗口：0043 -> 0042 放行（0040 迁移副本不构成新活动），随后升回 head。
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
    try:
        with pytest.raises(Exception, match="tenant_billing_downgrade_blocked"):
            alembic_downgrade(config, "0042_admin_idempotency_records")
        assert _current_version() == "0043_billing_downgrade_guard"
    finally:
        _run(
            _execute(
                "DELETE FROM tenant_wallet_transactions WHERE id = :id",
                {"id": marker_id},
            )
        )

    # 清理后同一 downgrade 再次放行。
    alembic_downgrade(config, "0042_admin_idempotency_records")
    assert _current_version() == "0042_admin_idempotency_records"
    alembic_upgrade(config, "head")
    assert _current_version() == "0043_billing_downgrade_guard"
