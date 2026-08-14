import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
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
    _guard_staged_billing_downgrade(connection)
    with context.begin_transaction():
        context.run_migrations()


_BILLING_LEDGER_REVISION = "0040_tenant_billing_usage"


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
