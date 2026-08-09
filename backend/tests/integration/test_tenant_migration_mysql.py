from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.session import engine


def test_tenant_control_plane_is_next_migration() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0037_tenant_control_plane"]


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
