"""add_google_drive_archive_credentials

Revision ID: 9a7b3c2d1e4f
Revises: d7e8f9a0b1c2
Create Date: 2026-11-01 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a7b3c2d1e4f"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    try:
        return column_name in {str(col.get("name") or "") for col in sa.inspect(op.get_bind()).get_columns(table_name)}
    except Exception:
        return False


def upgrade() -> None:
    for name in ("google_drive_refresh_token", "google_drive_folder_id"):
        if not _has_column("settings", name):
            op.add_column("settings", sa.Column(name, sa.String(), nullable=True))


def downgrade() -> None:
    pass
