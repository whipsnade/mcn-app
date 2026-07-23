"""Extend user_kol_favorites with platform/kol_uid identity."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# 注：revision 需 ≤32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "0021_favorite_platform_uid"
down_revision: str | None = "0020_kol_selection_session_rpts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_kol_favorites", sa.Column("platform", sa.String(32), nullable=True))
    op.add_column("user_kol_favorites", sa.Column("kol_uid", sa.String(128), nullable=True))
    op.add_column(
        "user_kol_favorites",
        sa.Column("nickname", sa.String(200), nullable=False, server_default=""),
    )
    op.add_column("user_kol_favorites", sa.Column("snapshot_json", sa.JSON(), nullable=True))
    op.alter_column("user_kol_favorites", "kol_id", existing_type=sa.String(36), nullable=True)
    op.create_unique_constraint(
        "uq_user_kol_favorites_user_platform_uid",
        "user_kol_favorites",
        ["user_id", "platform", "kol_uid"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_user_kol_favorites_user_platform_uid", "user_kol_favorites", type_="unique"
    )
    op.alter_column("user_kol_favorites", "kol_id", existing_type=sa.String(36), nullable=False)
    op.drop_column("user_kol_favorites", "snapshot_json")
    op.drop_column("user_kol_favorites", "nickname")
    op.drop_column("user_kol_favorites", "kol_uid")
    op.drop_column("user_kol_favorites", "platform")
