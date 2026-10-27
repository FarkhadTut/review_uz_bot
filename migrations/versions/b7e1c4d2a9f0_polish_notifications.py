"""notification flags

Revision ID: b7e1c4d2a9f0
Revises: 3454fbb593df
Create Date: 2026-10-27 10:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e1c4d2a9f0"
down_revision: str | Sequence[str] | None = "3454fbb593df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notify", sa.Boolean(), server_default="1", nullable=False))
    with op.batch_alter_table("draws", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("reminder_sent", sa.Boolean(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("missed_notified", sa.Boolean(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("notice_done", sa.Boolean(), server_default="0", nullable=False)
        )
    with op.batch_alter_table("draw_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notified", sa.Boolean(), server_default="0", nullable=False))
    # Draws that existed before these notices must not suddenly send them.
    op.execute("UPDATE draws SET notice_done = 1, reminder_sent = 1, missed_notified = 1")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("draw_entries", schema=None) as batch_op:
        batch_op.drop_column("notified")
    with op.batch_alter_table("draws", schema=None) as batch_op:
        batch_op.drop_column("notice_done")
        batch_op.drop_column("missed_notified")
        batch_op.drop_column("reminder_sent")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("notify")
