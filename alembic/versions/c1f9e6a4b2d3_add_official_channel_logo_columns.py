"""add_official_channel_logo_columns

Revision ID: c1f9e6a4b2d3
Revises: b8f4a7c9d321
Create Date: 2026-07-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1f9e6a4b2d3"
down_revision: Union[str, None] = "b8f4a7c9d321"
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
    if not _has_column("settings", "official_channel_logo_path"):
        op.add_column("settings", sa.Column("official_channel_logo_path", sa.String(), nullable=True))
    if not _has_column("settings", "official_channel_logo_url"):
        op.add_column("settings", sa.Column("official_channel_logo_url", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("official_channel_logo_url")
        batch_op.drop_column("official_channel_logo_path")
