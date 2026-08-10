"""Fail-closed downgrade guard for the tenant billing ledger (0040).

历史规则不允许回改已合入的 0040 迁移；本迁移不改变任何表结构，只在
downgrade 链最前端（0043 -> 0042 -> ... -> 0040）挂接一个无损校验：
一旦租户账本在 0040 之后产生了新流水/用量/余额漂移，继续 downgrade 会
静默丢失权威账本并恢复陈旧旧余额，因此必须 fail-closed。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0043_billing_downgrade_guard"
down_revision: str | None = "0042_admin_idempotency_records"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Guard-only migration: no schema change on the way up."""


def downgrade() -> None:
    connection = op.get_bind()
    # 1) 0040 之后新增的租户账本流水（迁移复制的行在旧表中有同 id 对应行）。
    new_ledger_rows = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenant_wallet_transactions t "
            "LEFT JOIN wallet_transactions w ON w.id = t.id "
            "WHERE w.id IS NULL"
        )
    ).scalar_one()
    # 2) 任何 Runtime 用量记录。
    usage_records = connection.execute(
        sa.text("SELECT COUNT(*) FROM runtime_usage_records")
    ).scalar_one()
    # 3) 已消耗或挂起的用户额度。
    quota_in_use = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenant_user_quota_usage "
            "WHERE spent <> 0 OR reserved <> 0"
        )
    ).scalar_one()
    # 4) 已迁移租户的钱包余额/预留与迁移校验副本发生漂移。
    balance_drift = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenant_wallets w "
            "JOIN tenant_billing_migration_validations v ON v.tenant_id = w.tenant_id "
            "WHERE w.balance <> v.copied_balance OR w.reserved <> v.copied_reserved"
        )
    ).scalar_one()
    # 5) 迁移后新建且非空的租户钱包（无校验副本可比对）。
    untracked_wallets = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenant_wallets w "
            "LEFT JOIN tenant_billing_migration_validations v ON v.tenant_id = w.tenant_id "
            "WHERE v.id IS NULL AND (w.balance <> 0 OR w.reserved <> 0)"
        )
    ).scalar_one()
    if new_ledger_rows or usage_records or quota_in_use or balance_drift or untracked_wallets:
        raise RuntimeError(
            "tenant_billing_downgrade_blocked: post-migration tenant ledger activity detected "
            f"(new_ledger_rows={new_ledger_rows}, usage_records={usage_records}, "
            f"quota_in_use={quota_in_use}, balance_drift={balance_drift}, "
            f"untracked_wallets={untracked_wallets}); refusing to drop the authoritative "
            "tenant billing tables and restore stale legacy balances"
        )
