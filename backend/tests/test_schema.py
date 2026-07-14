from app.db.base import Base
import app.db.models  # noqa: F401


def test_phase_one_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "users",
        "auth_identities",
        "user_sessions",
        "user_channel_permissions",
        "wallets",
        "wallet_transactions",
        "sessions",
        "messages",
    }
