"""add isolated YouTube channel accounts and project bindings

Revision ID: e2n2c4h5l6m7
Revises: f8a7b2c4d6e0
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2n2c4h5l6m7"
down_revision: Union[str, None] = "f8a7b2c4d6e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "youtube_channel_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("channel_title", sa.String(length=255), nullable=False),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "channel_id", name="uq_youtube_channel_accounts_user_channel"),
    )
    op.create_index("ix_youtube_channel_accounts_user_id", "youtube_channel_accounts", ["user_id"])
    op.create_table(
        "project_youtube_channel_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=False),
        sa.Column("youtube_channel_account_id", sa.Integer(), sa.ForeignKey("youtube_channel_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "project_key", name="uq_project_youtube_channel_user_project"),
    )
    op.create_index("ix_project_youtube_channel_bindings_user_id", "project_youtube_channel_bindings", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_project_youtube_channel_bindings_user_id", table_name="project_youtube_channel_bindings")
    op.drop_table("project_youtube_channel_bindings")
    op.drop_index("ix_youtube_channel_accounts_user_id", table_name="youtube_channel_accounts")
    op.drop_table("youtube_channel_accounts")
