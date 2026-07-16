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
