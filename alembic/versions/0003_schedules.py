"""employee schedules: weekly pattern + per-date overrides (day off / online / working day)

Revision ID: 0003_schedules
Revises: 0002_departments
Create Date: 2026-09-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_schedules"
down_revision: Union[str, None] = "0002_departments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_weekly_days",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),  # 0 = Monday ... 6 = Sunday
        sa.Column("kind", sa.String(length=16), nullable=False),  # off | online
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "weekday", name="uq_weekly_user_weekday"),
    )
    op.create_index("ix_weekly_user", "employee_weekly_days", ["user_id"])
    op.create_table(
        "employee_day_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),  # off | online | work
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day", name="uq_override_user_day"),
    )
    op.create_index("ix_override_day", "employee_day_overrides", ["day"])


def downgrade() -> None:
    op.drop_index("ix_override_day", table_name="employee_day_overrides")
    op.drop_table("employee_day_overrides")
    op.drop_index("ix_weekly_user", table_name="employee_weekly_days")
    op.drop_table("employee_weekly_days")
