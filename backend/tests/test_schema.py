from app.db.base import Base
import app.db.models  # noqa: F401


def test_phase_one_tables_are_registered() -> None:
    assert {
        "users",
        "auth_identities",
        "user_sessions",
        "user_channel_permissions",
        "wallets",
        "wallet_transactions",
        "sessions",
        "messages",
    }.issubset(Base.metadata.tables)


def test_phase_two_tables_are_registered() -> None:
    assert {
        "analysis_tasks",
        "task_events",
        "model_runs",
        "mcp_tool_catalog",
        "mcp_tool_discoveries",
        "mcp_calls",
        "kols",
        "kol_snapshots",
        "task_candidates",
        "task_candidate_pools",
        "task_candidate_pool_items",
        "bi_reports",
        "user_kol_favorites",
    }.issubset(Base.metadata.tables)


def test_sessions_register_soft_delete_column_and_visibility_index() -> None:
    sessions = Base.metadata.tables["sessions"]

    assert sessions.c.deleted_at.nullable is True
    assert ("user_id", "deleted_at", "last_accessed_at") in {
        tuple(column.name for column in index.columns) for index in sessions.indexes
    }


def test_analysis_tasks_register_persistent_creation_idempotency_index() -> None:
    tasks = Base.metadata.tables["analysis_tasks"]
    assert tasks.c.idempotency_key_hash.type.length == 64
    assert tasks.c.idempotency_payload_hash.type.length == 64
    assert any(
        index.unique
        and tuple(column.name for column in index.columns)
        == ("user_id", "session_id", "idempotency_key_hash")
        for index in tasks.indexes
    )


def test_agent_mode_columns_and_analysis_reports_table() -> None:
    tasks = Base.metadata.tables["analysis_tasks"]
    assert tasks.c.kind.nullable is False
    assert tasks.c.kind.type.length == 16

    sessions = Base.metadata.tables["sessions"]
    assert sessions.c.category.nullable is True

    reports = Base.metadata.tables["analysis_reports"]
    assert {"id", "task_id", "session_id", "version", "title", "blocks_json", "status"}.issubset(
        reports.c.keys()
    )
    assert any(
        tuple(column.name for column in constraint.columns) == ("task_id", "version")
        for constraint in reports.constraints
        if hasattr(constraint, "columns")
    )
