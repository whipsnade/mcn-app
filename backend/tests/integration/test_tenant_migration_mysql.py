from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.session import engine


def test_runtime_config_is_current_migration_head() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0044_agent_run_loop_guard"]


@pytest.mark.asyncio
async def test_tenant_migration_backfills_distinct_legacy_rows_and_non_null_run_scope() -> None:
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                row["name"]: row["nullable"]
                for row in inspect(sync).get_columns("agent_sessions")
                if row["name"] == "tenant_id"
            }
        )
        run_columns = await connection.run_sync(
            lambda sync: {
                row["name"]: row["nullable"]
                for row in inspect(sync).get_columns("agent_runs")
                if row["name"] == "tenant_id"
            }
        )
        assert columns == {"tenant_id": False}
        assert run_columns == {"tenant_id": False}
        counts = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) AS users, COUNT(DISTINCT m.tenant_id) AS tenants, "
                    "COUNT(m.user_id) AS memberships, COUNT(l.id) AS licenses "
                    "FROM users u LEFT JOIN tenant_memberships m ON m.user_id=u.id "
                    "LEFT JOIN tenants t ON t.id=m.tenant_id "
                    "LEFT JOIN tenant_licenses l ON l.id=t.active_license_id"
                )
            )
        ).mappings().one()
        run_session_mismatches = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM agent_runs r "
                "JOIN agent_sessions s ON s.id=r.session_id "
                "WHERE r.tenant_id <> s.tenant_id"
            )
        )
    assert counts["users"] == counts["memberships"] == counts["licenses"]
    assert counts["tenants"] == counts["users"]
    assert run_session_mismatches == 0


@pytest.mark.asyncio
async def test_runtime_config_migration_backfills_immutable_run_snapshot_columns() -> None:
    async with engine.connect() as connection:
        run_columns = await connection.run_sync(
            lambda sync: {
                row["name"]: row["nullable"]
                for row in inspect(sync).get_columns("agent_runs")
                if row["name"]
                in {
                    "runtime_backend",
                    "runtime_config_version_id",
                    "runtime_config_snapshot_json",
                    "queued_at",
                }
            }
        )
        config_count = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM runtime_config_versions "
                "WHERE id IN ('legacy-env-v1', 'poc-isolated-v1')"
            )
        )
        legacy_runs = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM agent_runs "
                "WHERE runtime_config_version_id='legacy-env-v1'"
            )
        )
    assert run_columns == {
        "runtime_backend": False,
        "runtime_config_version_id": False,
        "runtime_config_snapshot_json": False,
        "queued_at": False,
    }
    assert config_count == 2
    assert legacy_runs is not None
