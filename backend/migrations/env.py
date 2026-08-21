import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
import app.db.models  # noqa: F401


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    _ensure_alembic_version_capacity(connection)
    _guard_staged_billing_downgrade(connection)
    _guard_workbook_cache_key_downgrade(connection)
    with context.begin_transaction():
        context.run_migrations()


_BILLING_LEDGER_REVISION = "0040_tenant_billing_usage"
_WORKBOOK_CACHE_KEY_REVISION = "0046_workbook_export_cache_key"
_ALEMBIC_VERSION_CAPACITY = 64


def _ensure_alembic_version_capacity(connection) -> None:
    """让旧数据库能记录当前仓库的长 revision id。

    历史数据库把 ``alembic_version.version_num`` 建成了 VARCHAR(32)，而
    0047 已经是更长的审计迁移标识。这里仅扩展 Alembic 自身的 bookkeeping
    列，不修改业务表；没有 version 表时交给 Alembic 首次建表。
    """
    if connection.dialect.name != "mysql":
        return
    table_exists = connection.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'alembic_version'"
        )
    ).scalar()
    if not table_exists:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version ("
            "version_num VARCHAR(64) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
            ")"
        )
        return
    length = connection.execute(
        text(
            "SELECT CHARACTER_MAXIMUM_LENGTH "
            "FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'alembic_version' "
            "AND COLUMN_NAME = 'version_num'"
        )
    ).scalar()
    if length is not None and int(length) < _ALEMBIC_VERSION_CAPACITY:
        connection.exec_driver_sql(
            "ALTER TABLE alembic_version MODIFY version_num VARCHAR(64) NOT NULL"
        )


def _guard_staged_billing_downgrade(connection) -> None:
    """降穿 0040 的预检：staged downgrade 绕过 0043 guard 的封堵点。

    0043 的 guard 只在 head→0042 一步运行；先从 0043 降到 0042 再降穿
    0040 的链路不会经过它。这里在真正执行前预演步骤列表，凡是要执行
    0040 downgrade（drop 权威租户账本表）的命令一律先跑同一组检查。
    """
    migration_context = context.get_context()
    migrations_fn = getattr(migration_context, "_migrations_fn", None)
    if migrations_fn is None:
        return
    steps = list(migrations_fn(migration_context.get_current_heads(), migration_context))
    crossing = any(
        step.is_downgrade
        and getattr(getattr(step, "revision", None), "revision", None) == _BILLING_LEDGER_REVISION
        for step in steps
    )
    if not crossing:
        return
    from migrations.billing_downgrade_guard import assert_safe_to_drop_tenant_billing

    assert_safe_to_drop_tenant_billing(connection)


def _guard_workbook_cache_key_downgrade(connection) -> None:
    """禁止把已写入的长 Workbook cache key 降回 VARCHAR(32)。"""
    migration_context = context.get_context()
    migrations_fn = getattr(migration_context, "_migrations_fn", None)
    if migrations_fn is None:
        return
    steps = list(migrations_fn(migration_context.get_current_heads(), migration_context))
    crossing = any(
        step.is_downgrade
        and getattr(getattr(step, "revision", None), "revision", None)
        == _WORKBOOK_CACHE_KEY_REVISION
        for step in steps
    )
    if not crossing or connection.dialect.name != "mysql":
        return
    long_keys = connection.execute(
        text(
            "SELECT COUNT(*) FROM artifact_exports "
            "WHERE CHAR_LENGTH(template_version) > 32"
        )
    ).scalar()
    if int(long_keys or 0) > 0:
        raise RuntimeError("workbook_export_cache_key_downgrade_blocked")


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
