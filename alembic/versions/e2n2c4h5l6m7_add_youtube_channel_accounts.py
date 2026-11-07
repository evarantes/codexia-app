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


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _index_exists(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(table) and inspector.has_index(table, name)


def upgrade() -> None:
    # The Codexia ORM may create these tables during application bootstrap
    # before Alembic runs. Check first so the migration is safe in either order.
    if not _table_exists("youtube_channel_accounts"):
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
    if not _table_exists("project_youtube_channel_bindings"):
        op.create_table(
            "project_youtube_channel_bindings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_key", sa.String(length=64), nullable=False),
            sa.Column("youtube_channel_account_id", sa.Integer(), sa.ForeignKey("youtube_channel_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "project_key", name="uq_project_youtube_channel_user_project"),
        )
    if not _index_exists("youtube_channel_accounts", "ix_youtube_channel_accounts_user_id"):
        op.create_index("ix_youtube_channel_accounts_user_id", "youtube_channel_accounts", ["user_id"])
    if not _index_exists("project_youtube_channel_bindings", "ix_project_youtube_channel_bindings_user_id"):
        op.create_index("ix_project_youtube_channel_bindings_user_id", "project_youtube_channel_bindings", ["user_id"])


def downgrade() -> None:
    # Do not drop channel credentials/bindings: the ORM may have created the
    # tables before this revision, and a rollback must not erase connected data.
    pass
