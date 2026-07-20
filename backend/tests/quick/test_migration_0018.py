from sqlalchemy import inspect

from app.db.session import engine


async def test_users_industries_column_is_non_nullable_json() -> None:
    async with engine.connect() as connection:
        columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("users"))
    industries = next(item for item in columns if item["name"] == "industries")
    assert industries["nullable"] is False
    assert "JSON" in str(industries["type"]).upper()


async def test_quick_mcp_calls_table_contract() -> None:
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("quick_mcp_calls")
        )
        indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("quick_mcp_calls")
        )
        foreign_keys = await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("quick_mcp_calls")
        )

    names = {item["name"] for item in columns}
    assert {
        "id",
        "user_id",
        "feature",
        "internal_tool_name",
        "arguments_json",
        "status",
        "points_cost",
        "reserve_transaction_id",
        "settlement_transaction_id",
        "error_type",
        "created_at",
        "completed_at",
    } <= names
    assert "ix_quick_mcp_calls_user_created" in {item["name"] for item in indexes}
    targets = {
        tuple(item["constrained_columns"]): item["referred_table"] for item in foreign_keys
    }
    assert targets[("user_id",)] == "users"
    assert targets[("reserve_transaction_id",)] == "wallet_transactions"
    assert targets[("settlement_transaction_id",)] == "wallet_transactions"
