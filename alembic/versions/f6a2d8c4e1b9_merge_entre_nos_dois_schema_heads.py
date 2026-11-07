"""Merge Codexia's existing Drive schema with Entre Nós Dois settings.

Revision ID: f6a2d8c4e1b9
Revises: a91f4b6c8d2e, c8d4a7f1932b
"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = "f6a2d8c4e1b9"
down_revision: Union[str, Sequence[str], None] = (
    "a91f4b6c8d2e",
    "c8d4a7f1932b",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # This is a schema-only merge point; both parent migrations already ran.
    pass


def downgrade() -> None:
    # Keep the two established migration branches intact on downgrade.
    pass
