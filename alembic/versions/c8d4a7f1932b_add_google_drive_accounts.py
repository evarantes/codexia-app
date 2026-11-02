"""add Google Drive account pool for project-level archive sharding"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8d4a7f1932b"
down_revision: Union[str, None] = "9a7b3c2d1e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "google_drive_accounts" not in inspector.get_table_names():
        op.create_table(
            "google_drive_accounts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("label", sa.String(length=120), nullable=False),
            sa.Column("refresh_token", sa.String(), nullable=False),
            sa.Column("folder_id", sa.String(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    inspector = sa.inspect(op.get_bind())
    index_names = {
        str(index.get("name") or "")
        for index in inspector.get_indexes("google_drive_accounts")
    }
    if "ix_google_drive_accounts_id" not in index_names:
        op.create_index("ix_google_drive_accounts_id", "google_drive_accounts", ["id"])

    inspector = sa.inspect(op.get_bind())
    if "settings" not in inspector.get_table_names():
        return
    columns = {str(column.get("name") or "") for column in inspector.get_columns("settings")}
    if "google_drive_refresh_token" not in columns:
        return

    settings_columns = [
        sa.column("id", sa.Integer),
        sa.column("google_drive_refresh_token", sa.String),
    ]
    has_folder = "google_drive_folder_id" in columns
    if has_folder:
        settings_columns.append(sa.column("google_drive_folder_id", sa.String))
    settings = sa.table("settings", *settings_columns)
    selected = [settings.c.id, settings.c.google_drive_refresh_token]
    if has_folder:
        selected.append(settings.c.google_drive_folder_id)
    rows = op.get_bind().execute(
        sa.select(*selected)
        .where(settings.c.google_drive_refresh_token.is_not(None))
        .order_by(settings.c.id.desc())
    ).mappings().all()
    existing = op.get_bind().execute(
        sa.text("SELECT id FROM google_drive_accounts LIMIT 1")
    ).first()
    if not rows or existing:
        return

    # Preserve the account the old archive service selected by default. Old
    # Drive references without account IDs must continue resolving through it.
    row = rows[0]
    token = str(row["google_drive_refresh_token"] or "").strip()
    if not token:
        return
    op.get_bind().execute(
        sa.text(
            "INSERT INTO google_drive_accounts "
            "(label, refresh_token, folder_id, enabled, is_primary, is_legacy, created_at, updated_at) "
            "VALUES (:label, :token, :folder_id, TRUE, TRUE, TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "label": "Conta Google Drive existente",
            "token": token,
            "folder_id": row.get("google_drive_folder_id") if has_folder else None,
        },
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "google_drive_accounts" in inspector.get_table_names():
        indexes = {str(index.get("name") or "") for index in inspector.get_indexes("google_drive_accounts")}
        if "ix_google_drive_accounts_id" in indexes:
            op.drop_index("ix_google_drive_accounts_id", table_name="google_drive_accounts")
        op.drop_table("google_drive_accounts")
