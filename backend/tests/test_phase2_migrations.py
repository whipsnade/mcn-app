import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Integer, func, inspect, select

from app.db.base import Base
import app.db.models  # noqa: F401
from app.db.session import engine


def test_migration_chain_has_single_head() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0019_model_prompt_logs"]


async def test_phase_two_unique_constraints() -> None:
    async with engine.connect() as connection:
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("mcp_calls")
        )
    names = {item["name"] for item in constraints}
    assert "uq_mcp_calls_logical_call_id" in names
    assert "uq_mcp_calls_task_step_attempt" in names
    assert "uq_mcp_calls_settlement_transaction" in names


async def test_mcp_database_check_constraints_enforce_allowlists() -> None:
    async with engine.connect() as connection:
        catalog_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("mcp_tool_catalog")
        )
        call_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("mcp_calls")
        )
        discovery_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("mcp_tool_discoveries")
        )

    catalog_checks = {item["name"]: item["sqltext"] for item in catalog_constraints}
    call_checks = {item["name"]: item["sqltext"] for item in call_constraints}
    discovery_checks = {item["name"]: item["sqltext"] for item in discovery_constraints}
    service_check = catalog_checks["ck_mcp_tool_catalog_service_slug"]
    for service in {
        "insight-cube-mcp",
        "social-grow-mcp",
        "social-grow-content-mcp",
        "aktools-mcp",
        "bilibili-mcp",
    }:
        assert service in service_check
    for disabled_service in {
        "zhihu-mcp",
        "toutiao-mcp",
        "baidu-index-mcp",
        "google-trends-mcp",
    }:
        assert disabled_service not in service_check

    status_check = call_checks["ck_mcp_calls_status"]
    for status in {
        "planned",
        "reserved",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "settled",
        "released",
    }:
        assert status in status_check

    discovery_service_check = discovery_checks["ck_mcp_tool_discoveries_service_slug"]
    for service in {
        "insight-cube-mcp",
        "social-grow-mcp",
        "social-grow-content-mcp",
        "aktools-mcp",
        "bilibili-mcp",
    }:
        assert service in discovery_service_check
    discovery_status_check = discovery_checks["ck_mcp_tool_discoveries_review_status"]
    for status in {"quarantined", "approved", "rejected"}:
        assert status in discovery_status_check


async def test_mcp_discovery_service_remote_is_unique() -> None:
    async with engine.connect() as connection:
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("mcp_tool_discoveries")
        )
    assert "uq_mcp_tool_discoveries_service_remote" in {item["name"] for item in constraints}


async def test_mcp_billing_foreign_keys_and_recovery_indexes() -> None:
    async with engine.connect() as connection:
        foreign_keys = await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("mcp_calls")
        )
        call_indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("mcp_calls")
        )
        task_indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("analysis_tasks")
        )

    foreign_key_targets = {
        tuple(item["constrained_columns"]): item["referred_table"] for item in foreign_keys
    }
    assert foreign_key_targets[("reservation_transaction_id",)] == "wallet_transactions"
    assert foreign_key_targets[("settlement_transaction_id",)] == "wallet_transactions"
    assert foreign_key_targets[("task_id",)] == "analysis_tasks"

    assert "ix_mcp_calls_status_updated" in {item["name"] for item in call_indexes}
    assert {
        "ix_analysis_tasks_user_session_created",
        "ix_analysis_tasks_status_lease",
    }.issubset(item["name"] for item in task_indexes)


def test_reporting_version_columns_are_integer_in_metadata() -> None:
    version_columns = {
        ("task_candidates", "candidate_version"),
        ("bi_reports", "candidate_version"),
        ("bi_reports", "report_version"),
    }

    for table_name, column_name in version_columns:
        column = Base.metadata.tables[table_name].c[column_name]
        assert isinstance(column.type, Integer)
        assert column.nullable is False


async def test_reporting_version_columns_are_integer_in_mysql() -> None:
    async with engine.connect() as connection:
        task_candidate_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("task_candidates")
        )
        report_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("bi_reports")
        )

    columns = {("task_candidates", item["name"]): item for item in task_candidate_columns}
    columns.update((("bi_reports", item["name"]), item) for item in report_columns)
    for key in {
        ("task_candidates", "candidate_version"),
        ("bi_reports", "candidate_version"),
        ("bi_reports", "report_version"),
    }:
        assert isinstance(columns[key]["type"], Integer)
        assert columns[key]["nullable"] is False


async def test_candidate_versions_use_numeric_max_and_sorting() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = {name: str(uuid4()) for name in {"user", "session", "message", "task", "kol", "snapshot"}}
    tables = Base.metadata.tables

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                tables["users"].insert(),
                {
                    "id": ids["user"],
                    "nickname": "版本测试用户",
                    "role": "user",
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await connection.execute(
                tables["sessions"].insert(),
                {
                    "id": ids["session"],
                    "user_id": ids["user"],
                    "title": "版本测试",
                    "brand": "测试品牌",
                    "campaign_name": "版本排序",
                    "status": "active",
                    "platforms": ["bilibili"],
                    "category": "测试",
                    "target_audience": "测试受众",
                    "budget_min": None,
                    "budget_max": None,
                    "filters_snapshot": {},
                    "is_starred": False,
                    "last_accessed_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await connection.execute(
                tables["messages"].insert(),
                {
                    "id": ids["message"],
                    "session_id": ids["session"],
                    "user_id": ids["user"],
                    "role": "user",
                    "content": "生成候选",
                    "sequence": 1,
                    "metadata_json": {},
                    "created_at": now,
                },
            )
            await connection.execute(
                tables["analysis_tasks"].insert(),
                {
                    "id": ids["task"],
                    "user_id": ids["user"],
                    "session_id": ids["session"],
                    "trigger_message_id": ids["message"],
                    "status": "running",
                    "max_calls": 10,
                    "estimated_points": 0,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await connection.execute(
                tables["kols"].insert(),
                {
                    "id": ids["kol"],
                    "platform": "bilibili",
                    "platform_account_id": f"version-test-{ids['kol']}",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await connection.execute(
                tables["kol_snapshots"].insert(),
                {
                    "id": ids["snapshot"],
                    "kol_id": ids["kol"],
                    "normalized_json": {},
                    "collected_at": now,
                    "created_at": now,
                },
            )
            await connection.execute(
                tables["task_candidates"].insert(),
                [
                    {
                        "id": str(uuid4()),
                        "task_id": ids["task"],
                        "kol_id": ids["kol"],
                        "snapshot_id": ids["snapshot"],
                        "candidate_version": version,
                        "total_score": Decimal("80.000"),
                        "score_breakdown_json": {},
                        "rank": 1,
                        "matched_conditions_json": [],
                        "risk_flags_json": [],
                        "recommendation_text": "测试",
                        "evidence_json": {},
                        "created_at": now,
                    }
                    for version in (9, 10)
                ],
            )

            candidate = tables["task_candidates"]
            latest_version = await connection.scalar(
                select(func.max(candidate.c.candidate_version)).where(
                    candidate.c.task_id == ids["task"]
                )
            )
            versions = list(
                (
                    await connection.scalars(
                        select(candidate.c.candidate_version)
                        .where(candidate.c.task_id == ids["task"])
                        .order_by(candidate.c.candidate_version.desc())
                    )
                ).all()
            )
            assert latest_version == 10
            assert versions == [10, 9]
        finally:
            await transaction.rollback()


async def test_reporting_constraints_and_snapshot_contract() -> None:
    expected_unique_constraints = {
        "kols": "uq_kols_platform_account",
        "task_candidates": "uq_task_candidates_version_kol",
        "bi_reports": "uq_bi_reports_task_version",
        "user_kol_favorites": "uq_user_kol_favorites_user_kol",
    }

    async with engine.connect() as connection:
        for table_name, expected_name in expected_unique_constraints.items():
            constraints = await connection.run_sync(
                lambda sync, name=table_name: inspect(sync).get_unique_constraints(name)
            )
            assert expected_name in {item["name"] for item in constraints}

        snapshot_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("kol_snapshots")
        )
        snapshot_foreign_keys = await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("kol_snapshots")
        )
        snapshot_indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("kol_snapshots")
        )

    assert {"kol_id", "source_mcp_call_id", "normalized_json", "collected_at"}.issubset(
        item["name"] for item in snapshot_columns
    )
    snapshot_targets = {
        tuple(item["constrained_columns"]): item["referred_table"] for item in snapshot_foreign_keys
    }
    assert snapshot_targets[("kol_id",)] == "kols"
    assert snapshot_targets[("source_mcp_call_id",)] == "mcp_calls"
    assert "ix_kol_snapshots_kol_collected" in {item["name"] for item in snapshot_indexes}


def _run_alembic(*args: str) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    alembic = Path(sys.executable).with_name("alembic")
    subprocess.run(
        [str(alembic), *args],
        cwd=backend_dir,
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    "PYTEST_XDIST_WORKER" in os.environ,
    reason="schema migration boundary test is intentionally serial",
)
async def test_0005_mcp_tool_discoveries_migration_is_reversible() -> None:
    async def has_discovery_table() -> bool:
        async with engine.connect() as connection:
            return "mcp_tool_discoveries" in await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )

    try:
        _run_alembic("upgrade", "head")
        assert await has_discovery_table()

        _run_alembic("downgrade", "0004")
        assert not await has_discovery_table()

        _run_alembic("upgrade", "head")
        assert await has_discovery_table()
    finally:
        _run_alembic("upgrade", "head")


@pytest.mark.skipif(
    "PYTEST_XDIST_WORKER" in os.environ,
    reason="schema migration boundary test is intentionally serial",
)
async def test_0012_task_creation_order_migration_is_reversible() -> None:
    async def table_indexes() -> set[str]:
        async with engine.connect() as connection:
            return {
                item["name"]
                for item in await connection.run_sync(
                    lambda sync: inspect(sync).get_indexes("analysis_tasks")
                )
            }

    async def column_names() -> set[str]:
        async with engine.connect() as connection:
            return {
                item["name"]
                for item in await connection.run_sync(
                    lambda sync: inspect(sync).get_columns("analysis_tasks")
                )
            }

    try:
        _run_alembic("upgrade", "0011_session_soft_delete")
        _run_alembic("upgrade", "0012_task_creation_order")
        assert "creation_order" in await column_names()
        assert "ix_analysis_tasks_session_creation_order" in await table_indexes()

        _run_alembic("downgrade", "0011_session_soft_delete")
        assert "creation_order" not in await column_names()
        assert "ix_analysis_tasks_session_id" in await table_indexes()

        _run_alembic("upgrade", "0012_task_creation_order")
        assert "creation_order" in await column_names()
        indexes = await table_indexes()
        assert "ix_analysis_tasks_session_creation_order" in indexes
        assert "ix_analysis_tasks_session_id" not in indexes
    finally:
        _run_alembic("upgrade", "head")


@pytest.mark.skipif(
    "PYTEST_XDIST_WORKER" in os.environ,
    reason="schema migration boundary test is intentionally serial",
)
async def test_phase_two_migration_table_boundaries_restore_head() -> None:
    phase_one_tables = {
        "users",
        "auth_identities",
        "user_sessions",
        "user_channel_permissions",
        "wallets",
        "wallet_transactions",
        "sessions",
        "messages",
    }
    runtime_tables = {
        "analysis_tasks",
        "task_events",
        "model_runs",
        "mcp_tool_catalog",
        "mcp_calls",
    }
    late_runtime_tables = {"mcp_tool_discoveries"}
    reporting_tables = {
        "kols",
        "kol_snapshots",
        "task_candidates",
        "bi_reports",
        "user_kol_favorites",
    }
    phase_two_tables = runtime_tables | late_runtime_tables | reporting_tables

    async def table_names() -> set[str]:
        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))

    try:
        _run_alembic("downgrade", "0001")
        tables_at_0001 = await table_names()
        assert phase_one_tables.issubset(tables_at_0001)
        assert phase_two_tables.isdisjoint(tables_at_0001)

        _run_alembic("upgrade", "0002")
        tables_at_0002 = await table_names()
        assert phase_one_tables.issubset(tables_at_0002)
        assert runtime_tables.issubset(tables_at_0002)
        assert late_runtime_tables.isdisjoint(tables_at_0002)
        assert reporting_tables.isdisjoint(tables_at_0002)

        _run_alembic("upgrade", "head")
        tables_at_head = await table_names()
        assert phase_one_tables.issubset(tables_at_head)
        assert phase_two_tables.issubset(tables_at_head)
    finally:
        _run_alembic("upgrade", "head")
