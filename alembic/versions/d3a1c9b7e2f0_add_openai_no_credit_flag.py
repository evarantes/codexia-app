"""add_openai_no_credit_flag

Revision ID: d3a1c9b7e2f0
Revises: cf47b0b001c3
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3a1c9b7e2f0"
down_revision: Union[str, None] = "cf47b0b001c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table_name: str, column_name: str) -> bool:
    try:
        return column_name in {str(col.get("name") or "") for col in _inspector().get_columns(table_name)}
    except Exception:
        return False


def upgrade() -> None:
    if not _has_column("settings", "openai_no_credit"):
        op.add_column("settings", sa.Column("openai_no_credit", sa.Boolean(), nullable=True, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("settings", "openai_no_credit")
