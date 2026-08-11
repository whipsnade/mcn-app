import os
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Integer, func, inspect, select, text

from app.db.base import Base
import app.db.models  # noqa: F401
from app.db.session import engine


@pytest.fixture(scope="module", autouse=True)
def _clean_tenant_billing_residue():
    """0043 downgrade guard 的干净窗口。

    提交落库的集成测试（真实拓扑 UAT、agent runtime real 等）会经 B4 懒
    provisioning / welcome grant 在租户账本表留下已提交残留；0043 的
    downgrade guard 按设计会因此 fail-closed。迁移可逆性测试与这些残留
    无关，运行前清掉；迁移副本（旧表同 id 行、validation 副本）保留。
    """

    async def _clean() -> None:
        async with engine.begin() as connection:
            exists = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tenant_wallets'"
                )
            )
            if not exists:
                # 库停在中段迁移版本（如 0037，B4 表尚未创建）时无可清理。
                return
        # 先兜底清除任何已提交 UAT 残留（含非 legacy slug 租户，0037 guard）。
        from tests.integration.pi_uat.harness import purge_uat_residue

        await purge_uat_residue()
        async with engine.begin() as connection:
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
            # 提交测试遗留的 legacy 钱包孤儿行（用户已删或无 membership）会让
            # 0040 升级的 tenant_billing_wallet_orphan 校验 fail-closed。
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

    asyncio.run(_clean())
    yield


def test_migration_chain_has_single_head() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0043_billing_downgrade_guard"]


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


async def test_0020_analysis_reports_session_scoped_schema() -> None:
    """迁移 0020：analysis_reports 支持会话级报告。"""
    async with engine.connect() as connection:
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("analysis_reports")
        )
        columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("analysis_reports")
        )
    names = {item["name"] for item in constraints}
    # (session_id, version) 唯一约束由 0022 升级为 (session_id, report_type, version)。
    assert "uq_analysis_reports_session_type_version" in names
    assert "uq_analysis_reports_task_version" in names
    task_id_column = {item["name"]: item for item in columns}["task_id"]
    assert task_id_column["nullable"] is True


async def test_0020_session_kol_selections_schema() -> None:
    """迁移 0020：session_kol_selections 圈选名单表结构。"""
    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("session_kol_selections")
        )
        indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("session_kol_selections")
        )
    assert "session_kol_selections" in table_names
    assert "uq_kol_selection_session_platform_uid" in {item["name"] for item in constraints}
    assert "ix_kol_selection_user" in {item["name"] for item in indexes}


async def test_0021_user_kol_favorites_platform_uid_schema() -> None:
    """迁移 0021：user_kol_favorites 扩展 platform/kol_uid 身份列。"""
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("user_kol_favorites")
        )
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("user_kol_favorites")
        )
    by_name = {item["name"]: item for item in columns}
    for name in ("platform", "kol_uid", "nickname", "snapshot_json"):
        assert name in by_name
    assert by_name["kol_id"]["nullable"] is True
    assert by_name["nickname"]["nullable"] is False
    names = {item["name"] for item in constraints}
    assert "uq_user_kol_favorites_user_platform_uid" in names
    assert "uq_user_kol_favorites_user_kol" in names


async def test_0022_goal_artifact_infra_schema() -> None:
    """迁移 0022：goal/artifact 基础设施新表与 analysis_reports/mcp_calls 扩展列。"""
    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        goal_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("task_goals")
        )
        artifact_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("task_artifacts")
        )
        artifact_indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("task_artifacts")
        )
        set_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("kol_selection_sets")
        )
        item_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("kol_selection_items")
        )
        item_indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("kol_selection_items")
        )
        read_state_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("artifact_read_states")
        )
        brand_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("user_brand_profiles")
        )
        brand_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("user_brand_profiles")
        )
        report_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("analysis_reports")
        )
        report_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("analysis_reports")
        )
        mcp_call_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("mcp_calls")
        )

    for table in (
        "task_goals",
        "task_artifacts",
        "kol_selection_sets",
        "kol_selection_items",
        "artifact_read_states",
        "user_brand_profiles",
    ):
        assert table in table_names

    assert "uq_task_goals_task_sequence" in {item["name"] for item in goal_constraints}
    assert "uq_task_artifacts_artifact_key" in {item["name"] for item in artifact_constraints}
    assert "ix_task_artifacts_session_type" in {item["name"] for item in artifact_indexes}
    assert "uq_kol_selection_sets_session_version" in {item["name"] for item in set_constraints}
    assert "uq_kol_selection_items_set_platform_uid" in {
        item["name"] for item in item_constraints
    }
    assert "ix_kol_selection_items_user" in {item["name"] for item in item_indexes}
    assert "uq_artifact_read_states_user_session_module" in {
        item["name"] for item in read_state_constraints
    }

    brand_names = {item["name"] for item in brand_constraints}
    assert "uq_user_brand_profiles_user_brand" in brand_names
    assert "uq_user_brand_profiles_user_default" in brand_names
    brand_by_name = {item["name"]: item for item in brand_columns}
    assert brand_by_name["is_default"]["nullable"] is True

    report_names = {item["name"] for item in report_constraints}
    assert "uq_analysis_reports_session_type_version" in report_names
    assert "uq_analysis_reports_session_version" not in report_names
    report_by_name = {item["name"]: item for item in report_columns}
    assert report_by_name["report_type"]["nullable"] is False
    assert report_by_name["scope_json"]["nullable"] is True

    mcp_call_by_name = {item["name"]: item for item in mcp_call_columns}
    assert mcp_call_by_name["goal_id"]["nullable"] is True


async def test_0027_agent_runtime_v3_schema() -> None:
    """迁移 0027：统一 Agent 运行时新表与 artifact_read_states 扩展列。"""
    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        artifact_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("agent_artifacts")
        )
        draft_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("artifact_drafts")
        )
        step_foreign_keys = await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("agent_steps")
        )
        version_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("agent_artifact_versions")
        )
        tool_call_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("agent_tool_calls")
        )
        reconciliation_foreign_keys = await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("agent_tool_call_reconciliations")
        )
        cache_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("kol_detail_cache")
        )
        read_state_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("artifact_read_states")
        )
        read_state_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("artifact_read_states")
        )

    for table in (
        "agent_sessions",
        "agent_messages",
        "agent_runs",
        "agent_run_attempts",
        "agent_steps",
        "agent_tool_calls",
        "evidence_items",
        "agent_events",
        "agent_tool_call_reconciliations",
        "memory_entries",
        "agent_artifacts",
        "artifact_drafts",
        "artifact_draft_revisions",
        "artifact_review_batches",
        "artifact_review_items",
        "artifact_review_attempts",
        "agent_artifact_versions",
        "artifact_events",
        "kol_detail_cache",
    ):
        assert table in table_names

    assert "uq_agent_artifacts_session_key" in {item["name"] for item in artifact_constraints}
    assert "uq_artifact_drafts_artifact" in {item["name"] for item in draft_constraints}

    step_fk_targets = {
        tuple(item["constrained_columns"]): item["referred_table"] for item in step_foreign_keys
    }
    assert step_fk_targets[("attempt_id",)] == "agent_run_attempts"

    version_by_name = {item["name"]: item for item in version_columns}
    assert "source_draft_revision_id" in version_by_name
    assert "parent_artifact_version_id" in version_by_name

    assert "uq_agent_tool_calls_logical_call_id" in {
        item["name"] for item in tool_call_constraints
    }
    reconciliation_targets = {
        tuple(item["constrained_columns"]): item["referred_table"]
        for item in reconciliation_foreign_keys
    }
    assert reconciliation_targets[("tool_call_id",)] == "agent_tool_calls"

    assert "uq_kol_detail_cache_user_session_platform_kol" in {
        item["name"] for item in cache_constraints
    }

    read_state_names = {item["name"] for item in read_state_constraints}
    assert "uq_artifact_read_states_user_session_module_v2" in read_state_names
    assert "uq_artifact_read_states_user_session_module" in read_state_names
    v2_constraint = next(
        item
        for item in read_state_constraints
        if item["name"] == "uq_artifact_read_states_user_session_module_v2"
    )
    assert v2_constraint["column_names"] == ["user_id", "session_id", "module"]
    read_state_by_name = {item["name"]: item for item in read_state_columns}
    for column in ("module", "last_seen_sequence", "updated_at"):
        assert column in read_state_by_name
    for column in ("module_key", "last_seen_artifact_id", "seen_at"):
        assert column in read_state_by_name


async def test_0028_agent_artifact_read_states_schema() -> None:
    """迁移 0028：agent_artifact_read_states 独立新表 + versions 增 lineage_snapshot_json。

    旧 artifact_read_states 表保持不动（保留旧应用版本回滚能力），不迁移旧水位。
    """
    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        read_state_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("agent_artifact_read_states")
        )
        read_state_foreign_keys = await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("agent_artifact_read_states")
        )
        read_state_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("agent_artifact_read_states")
        )
        version_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("agent_artifact_versions")
        )
        legacy_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("artifact_read_states")
        )

    assert "agent_artifact_read_states" in table_names
    # 旧表保留不动
    assert "artifact_read_states" in table_names
    legacy_names = {item["name"] for item in legacy_constraints}
    assert "uq_artifact_read_states_user_session_module" in legacy_names
    assert "uq_artifact_read_states_user_session_module_v2" in legacy_names

    constraint_by_name = {item["name"]: item for item in read_state_constraints}
    new_unique = constraint_by_name["uq_agent_artifact_read_states_user_session_module"]
    assert new_unique["column_names"] == ["user_id", "session_id", "module"]

    fk_targets = {
        tuple(item["constrained_columns"]): item["referred_table"]
        for item in read_state_foreign_keys
    }
    assert fk_targets[("session_id",)] == "agent_sessions"

    columns_by_name = {item["name"]: item for item in read_state_columns}
    for column in ("id", "user_id", "session_id", "module", "last_seen_sequence", "updated_at"):
        assert column in columns_by_name
        assert columns_by_name[column]["nullable"] is False

    version_by_name = {item["name"]: item for item in version_columns}
    assert "lineage_snapshot_json" in version_by_name
    assert version_by_name["lineage_snapshot_json"]["nullable"] is True


async def test_0031_upload_and_evidence_schema() -> None:
    """迁移 0031：agent_uploads 表 + evidence_items 上传/归一化诊断列。"""
    assert {
        "id",
        "user_id",
        "session_id",
        "run_id",
        "original_filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "storage_key",
        "status",
        "error_code",
        "created_at",
        "completed_at",
    } <= await column_names("agent_uploads")

    evidence = await column_names("evidence_items")
    assert {
        "upload_id",
        "normalization_version",
        "normalization_status",
        "field_mapping_json",
        "unmapped_fields_json",
        "truncated",
        "normalization_error_code",
    } <= evidence

    async with engine.connect() as connection:
        check_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("evidence_items")
        )
        evidence_indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("evidence_items")
        )
        upload_columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("agent_uploads")
        )

    checks = {item["name"]: item["sqltext"] for item in check_constraints}
    assert "ck_evidence_items_tool_call_xor_upload" in checks
    xor_text = checks["ck_evidence_items_tool_call_xor_upload"].lower()
    assert "tool_call_id" in xor_text and "upload_id" in xor_text and "null" in xor_text

    index_names = {item["name"] for item in evidence_indexes}
    assert "ix_evidence_items_session_collected_at" in index_names
    assert "ix_evidence_items_upload_id" in index_names

    column_by_name = {item["name"]: item for item in upload_columns}
    assert column_by_name["run_id"]["nullable"] is True
    assert column_by_name["error_code"]["nullable"] is True
    assert column_by_name["completed_at"]["nullable"] is True
    assert column_by_name["status"]["nullable"] is False


async def column_names(table: str) -> set[str]:
    async with engine.connect() as connection:
        return {
            item["name"]
            for item in await connection.run_sync(
                lambda sync: inspect(sync).get_columns(table)
            )
        }


async def test_0030_direct_publish_schema() -> None:
    """迁移 0030：直接发布尝试表 + versions.validation_json + confirmed_scope 记忆类型。"""
    columns = await column_names("artifact_publish_attempts")
    assert {
        "id",
        "run_id",
        "artifact_id",
        "draft_revision_id",
        "status",
        "idempotency_key",
        "validation_json",
        "error_code",
        "published_version_id",
        "created_at",
        "completed_at",
    } <= columns
    assert "validation_json" in await column_names("agent_artifact_versions")

    async with engine.connect() as connection:
        unique_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("artifact_publish_attempts")
        )
        check_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("artifact_publish_attempts")
        )
        memory_checks = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("memory_entries")
        )
        attempt_indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("artifact_publish_attempts")
        )

    assert "uq_artifact_publish_attempts_idempotency" in {
        item["name"] for item in unique_constraints
    }
    # run_id 索引支撑终态聚合按 run_id 扫描（Gate A 审查：缺少索引）。
    assert "ix_artifact_publish_attempts_run_id" in {
        item["name"] for item in attempt_indexes
    }
    checks = {item["name"]: item["sqltext"] for item in check_constraints}
    status_check = checks["ck_artifact_publish_attempts_status"]
    for status in ("validating", "published", "validation_failed", "failed"):
        assert status in status_check
    memory_check = {item["name"]: item["sqltext"] for item in memory_checks}[
        "ck_memory_entries_type"
    ]
    assert "confirmed_scope" in memory_check


@pytest.mark.skipif(
    "PYTEST_XDIST_WORKER" in os.environ,
    reason="schema migration boundary test is intentionally serial",
)
async def test_0030_direct_publish_migration_is_reversible_with_confirmed_scope() -> None:
    """0030 downgrade 必须先清除已落库的 confirmed_scope 行（Gate A 审查修复）。

    MySQL 重建 ``ck_memory_entries_type`` CHECK 约束时会校验既有行，残留的
    confirmed_scope 行会让 downgrade 失败。本测试先落一条 confirmed_scope
    记忆，再 downgrade 到 0029，确认成功且 0030 新增对象消失，最后回到 head。
    """

    async def has_publish_attempts_table() -> bool:
        async with engine.connect() as connection:
            return "artifact_publish_attempts" in await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )

    async def has_validation_json_column() -> bool:
        async with engine.connect() as connection:
            return "validation_json" in {
                item["name"]
                for item in await connection.run_sync(
                    lambda sync: inspect(sync).get_columns("agent_artifact_versions")
                )
            }

    # 落一条 confirmed_scope 记忆（需要 user + session 满足 FK）。
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.identity.models import User
    from app.agent_runtime.models import AgentSession, MemoryEntry

    user_id = str(uuid4())
    session_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(
            id=user_id,
            nickname="reversibility-probe",
            role="user",
            status="active",
            industries=["美食"],
            created_at=now,
            updated_at=now,
        )
        agent_session = AgentSession(
            id=session_id,
            user_id=user.id,
            title="reversibility",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        session.add(agent_session)
        await session.flush()
        session.add(
            MemoryEntry(
                id=str(uuid4()),
                session_id=agent_session.id,
                source_run_id=None,
                memory_type="confirmed_scope",
                content_json={"domain": "brand", "field": "period", "value": "近30天"},
                created_at=now,
            )
        )
        await session.commit()

    try:
        assert await has_publish_attempts_table()
        assert await has_validation_json_column()
        # downgrade 必须成功：confirmed_scope 行已被迁移先清除。
        _run_alembic("downgrade", "0029_agent_run_created_at")
        assert not await has_publish_attempts_table()
        assert not await has_validation_json_column()
        _run_alembic("upgrade", "head")
        assert await has_publish_attempts_table()
        assert await has_validation_json_column()
    finally:
        _run_alembic("upgrade", "head")
        # 清理测试插入的 user/session 行，避免残留污染后续用例。
        from sqlalchemy import delete

        async with engine.begin() as connection:
            await connection.execute(
                delete(AgentSession).where(AgentSession.id == session_id)
            )
            await connection.execute(delete(User).where(User.id == user_id))


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


async def test_0035_artifact_exports_schema() -> None:
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                row["name"]: row["type"] for row in inspect(sync).get_columns("artifact_exports")
            }
        )
    assert {
        "id",
        "artifact_version_id",
        "template_version",
        "status",
        "filename",
        "storage_key",
        "sha256",
        "size_bytes",
        "error_code",
        "created_at",
        "completed_at",
    } <= set(columns)
    async with engine.connect() as connection:
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints("artifact_exports")
        )
    names = {item["name"] for item in constraints}
    assert "uq_artifact_exports_version_template" in names


async def test_0036_export_claim_token_reversible() -> None:
    """0036 upgrade → downgrade → upgrade：claim_token 列可逆（加列/删列不丢表）。"""

    async def export_columns() -> set[str]:
        async with engine.connect() as connection:
            return {
                item["name"]
                for item in await connection.run_sync(
                    lambda sync: inspect(sync).get_columns("artifact_exports")
                )
            }

    assert "claim_token" in await export_columns()
    try:
        _run_alembic("downgrade", "0035_artifact_exports")
        assert "claim_token" not in await export_columns()
        # 既有表与唯一约束仍在（downgrade 只删列，不删表）。
        assert {"id", "status", "created_at"} <= await export_columns()
        _run_alembic("upgrade", "head")
        assert "claim_token" in await export_columns()
    finally:
        _run_alembic("upgrade", "head")


async def test_0034_dispatch_count_reversible() -> None:
    """0034 数据级 upgrade → downgrade → upgrade：dispatch_count 列可逆。"""
    from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession, AgentStep, AgentToolCall
    from sqlalchemy.ext.asyncio import AsyncSession

    async def has_dispatch_count() -> bool:
        async with engine.connect() as connection:
            return "dispatch_count" in {
                item["name"]
                for item in await connection.run_sync(
                    lambda sync: inspect(sync).get_columns("agent_tool_calls")
                )
            }

    # 创建一条调用行做数据级测试
    user_id = str(uuid4())
    session_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        from app.identity.models import User
        user = User(id=user_id, nickname="dispatch-test", role="user", status="active",
                    industries=["美食"], created_at=now, updated_at=now)
        ag_session = AgentSession(id=session_id, user_id=user_id, title="dispatch",
                                   status="active", created_at=now, updated_at=now)
        session.add(user)
        await session.flush()
        session.add(ag_session)
        await session.flush()
        run = AgentRun(id=str(uuid4()), session_id=session_id, user_id=user_id,
                       run_kind="user", visibility="user", profile_name="session_analyst_v1",
                       profile_version="v1", model="test", status="running", decision_count=0,
                       review_count=0, revision_count=0, started_at=now)
        session.add(run)
        await session.flush()
        attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
        session.add(attempt)
        await session.flush()
        step = AgentStep(id=str(uuid4()), run_id=run.id, attempt_id=attempt.id, sequence=1,
                         step_type="tool_call", status="running", visibility="user", created_at=now)
        session.add(step)
        await session.flush()
        call = AgentToolCall(id=str(uuid4()), run_id=run.id, step_id=step.id,
                             logical_call_id="test-dispatch-" + str(uuid4()),
                             service="insight-cube-mcp", internal_tool_name="query_analysis_data",
                             arguments_json={}, arguments_hash="a"*64, status="settled",
                             points_reserved=0, points_settled=10, dispatch_count=1)
        session.add(call)
        await session.commit()

    try:
        assert await has_dispatch_count() is True
        # downgrade → 0033：dispatch_count 列消失
        _run_alembic("downgrade", "0033_safe_error_msg_text")
        assert await has_dispatch_count() is False
        # upgrade → head：dispatch_count 列恢复
        _run_alembic("upgrade", "head")
        assert await has_dispatch_count() is True
    finally:
        # 清理测试数据
        async with engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM agent_tool_calls WHERE run_id = '{run.id}'"))
            await conn.execute(text(f"DELETE FROM agent_steps WHERE run_id = '{run.id}'"))
            await conn.execute(text(f"DELETE FROM agent_run_attempts WHERE run_id = '{run.id}'"))
            await conn.execute(text(f"DELETE FROM agent_runs WHERE id = '{run.id}'"))
            await conn.execute(text(f"DELETE FROM agent_sessions WHERE id = '{session_id}'"))
            await conn.execute(text(f"DELETE FROM users WHERE id = '{user_id}'"))


async def test_0034_dangerous_downgrade_refused() -> None:
    """存在 dispatch_count != 1 的调用行时，0034 downgrade 被拒绝（防状态丢失）。"""
    from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession, AgentStep, AgentToolCall
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = str(uuid4())
    session_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    run_id = str(uuid4())
    async with AsyncSession(engine, expire_on_commit=False) as session:
        from app.identity.models import User
        user = User(id=user_id, nickname="danger-downgrade", role="user", status="active",
                    industries=["美食"], created_at=now, updated_at=now)
        ag_session = AgentSession(id=session_id, user_id=user_id, title="danger",
                                   status="active", created_at=now, updated_at=now)
        session.add(user)
        await session.flush()
        session.add(ag_session)
        await session.flush()
        run = AgentRun(id=run_id, session_id=session_id, user_id=user_id,
                       run_kind="user", visibility="user", profile_name="session_analyst_v1",
                       profile_version="v1", model="test", status="running", decision_count=0,
                       review_count=0, revision_count=0, started_at=now)
        session.add(run)
        await session.flush()
        attempt = AgentRunAttempt(id=str(uuid4()), run_id=run_id, attempt=1, started_at=now)
        session.add(attempt)
        await session.flush()
        step = AgentStep(id=str(uuid4()), run_id=run_id, attempt_id=attempt.id, sequence=1,
                         step_type="tool_call", status="running", visibility="user", created_at=now)
        session.add(step)
        await session.flush()
        call = AgentToolCall(id=str(uuid4()), run_id=run_id, step_id=step.id,
                             logical_call_id="danger-" + str(uuid4()),
                             service="insight-cube-mcp", internal_tool_name="query_analysis_data",
                             arguments_json={}, arguments_hash="c"*64, status="failed",
                             points_reserved=0, points_settled=0, dispatch_count=2,
                             error_type="definitely_not_sent")
        session.add(call)
        await session.commit()

    try:
        with pytest.raises(subprocess.CalledProcessError):
            _run_alembic("downgrade", "0033_safe_error_msg_text")
        # 调用行未丢失
        async with engine.connect() as conn:
            remaining = (await conn.execute(
                text("SELECT COUNT(*) FROM agent_tool_calls WHERE run_id = :rid"),
                {"rid": run_id},
            )).scalar()
            assert remaining == 1
    finally:
        _run_alembic("upgrade", "head")
        async with engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM agent_tool_calls WHERE run_id = '{run_id}'"))
            await conn.execute(text(f"DELETE FROM agent_steps WHERE run_id = '{run_id}'"))
            await conn.execute(text(f"DELETE FROM agent_run_attempts WHERE run_id = '{run_id}'"))
            await conn.execute(text(f"DELETE FROM agent_runs WHERE id = '{run_id}'"))
            await conn.execute(text(f"DELETE FROM agent_sessions WHERE id = '{session_id}'"))
            await conn.execute(text(f"DELETE FROM users WHERE id = '{user_id}'"))
