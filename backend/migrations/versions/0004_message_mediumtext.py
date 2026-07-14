"""Use MEDIUMTEXT for message content."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "content",
        existing_type=sa.Text(),
        type_=mysql.MEDIUMTEXT(),
        existing_nullable=False,
    )


def downgrade() -> None:
    oversized_count = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM messages "
            "WHERE OCTET_LENGTH(content) > 65535"
        )
    ).scalar_one()
    if oversized_count:
        raise RuntimeError(
            "message_content_exceeds_text_limit: "
            f"{oversized_count} messages exceed the 65535-byte TEXT limit"
        )

    op.alter_column(
        "messages",
        "content",
        existing_type=mysql.MEDIUMTEXT(),
        type_=sa.Text(),
        existing_nullable=False,
    )
