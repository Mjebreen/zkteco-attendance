"""employee vacations (date ranges)

Revision ID: 0004_vacations
Revises: 0003_schedules
Create Date: 2026-09-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_vacations"
down_revision: Union[str, None] = "0003_schedules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_vacations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("start_day", sa.Date(), nullable=False),
        sa.Column("end_day", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vacations_user", "employee_vacations", ["user_id"])
    op.create_index("ix_vacations_range", "employee_vacations", ["start_day", "end_day"])


def downgrade() -> None:
    op.drop_index("ix_vacations_range", table_name="employee_vacations")
    op.drop_index("ix_vacations_user", table_name="employee_vacations")
    op.drop_table("employee_vacations")
