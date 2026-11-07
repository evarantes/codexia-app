"""add fal.ai credential to user settings

Revision ID: a91f4b6c8d2e
Revises: e2n2c4h5l6m7
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a91f4b6c8d2e"
down_revision: Union[str, None] = "e2n2c4h5l6m7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("settings", sa.Column("fal_api_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("settings", "fal_api_key")
