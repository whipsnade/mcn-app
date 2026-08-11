"""租户账本 downgrade guard 的共享判定（供 env.py 预检调用）。

0043 迁移的 downgrade 里内联了同一组检查（已合入的历史迁移不可改）；本
模块供 env.py 在任何「降穿 0040」的命令（包括 staged downgrade 绕过
0043 的场景）执行前预检，两处语义必须保持一致。
"""

from __future__ import annotations

import sqlalchemy as sa


def assert_safe_to_drop_tenant_billing(connection) -> None:
    """0040 之后有新流水/用量/余额漂移时拒绝降穿 0040。"""
    new_ledger_rows = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenant_wallet_transactions t "
            "LEFT JOIN wallet_transactions w ON w.id = t.id "
            "WHERE w.id IS NULL"
        )
    ).scalar_one()
    usage_records = connection.execute(
        sa.text("SELECT COUNT(*) FROM runtime_usage_records")
    ).scalar_one()
    quota_in_use = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenant_user_quota_usage "
            "WHERE spent <> 0 OR reserved <> 0"
        )
    ).scalar_one()
    balance_drift = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenant_wallets w "
            "JOIN tenant_billing_migration_validations v ON v.tenant_id = w.tenant_id "
            "WHERE w.balance <> v.copied_balance OR w.reserved <> v.copied_reserved"
        )
    ).scalar_one()
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
