"""manual punch corrections layered on top of the device records

Revision ID: 0007_punch_corrections
Revises: 0006_branding
Create Date: 2026-09-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007_punch_corrections"
down_revision: Union[str, None] = "0006_branding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "punch_corrections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),  # add | void
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", "timestamp", name="uq_correction_user_kind_ts"),
    )
    op.create_index("ix_corrections_ts", "punch_corrections", ["timestamp"])
    op.create_index("ix_corrections_user", "punch_corrections", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_corrections_user", table_name="punch_corrections")
    op.drop_index("ix_corrections_ts", table_name="punch_corrections")
    op.drop_table("punch_corrections")
