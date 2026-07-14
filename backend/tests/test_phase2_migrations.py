from sqlalchemy import inspect

from app.db.session import engine


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

    catalog_checks = {item["name"]: item["sqltext"] for item in catalog_constraints}
    call_checks = {item["name"]: item["sqltext"] for item in call_constraints}
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
