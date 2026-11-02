"""Add dedicated Google Drive OAuth client credentials."""
from alembic import op
import sqlalchemy as sa

revision = "f14a3b7c9d20"
down_revision = "c8d4a7f1932b"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("settings")}
    if "google_drive_client_id" not in columns:
        op.add_column("settings", sa.Column("google_drive_client_id", sa.String(), nullable=True))
    if "google_drive_client_secret" not in columns:
        op.add_column("settings", sa.Column("google_drive_client_secret", sa.String(), nullable=True))


def downgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("settings")}
    if "google_drive_client_secret" in columns:
        op.drop_column("settings", "google_drive_client_secret")
    if "google_drive_client_id" in columns:
        op.drop_column("settings", "google_drive_client_id")
