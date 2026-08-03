"""reconcile_settings_missing_columns

Revision ID: c60c7aca98ee
Revises: d3a1c9b7e2f0
Create Date: 2026-08-01 11:59:46.413570

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c60c7aca98ee'
down_revision: Union[str, None] = 'd3a1c9b7e2f0'
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
    table_name = "settings"

    for column_name, column in [
        ("per_video_spend_limit", sa.Column("per_video_spend_limit", sa.Float(), nullable=True)),
        ("max_quality_recovery_attempts", sa.Column("max_quality_recovery_attempts", sa.Integer(), nullable=True)),
        ("min_quality_recovery_score_delta", sa.Column("min_quality_recovery_score_delta", sa.Float(), nullable=True)),
        ("primary_provider", sa.Column("primary_provider", sa.String(), nullable=True)),
        ("fallback_provider", sa.Column("fallback_provider", sa.String(), nullable=True)),
        ("editorial_provider", sa.Column("editorial_provider", sa.String(), nullable=True)),
        ("editorial_fallback_provider", sa.Column("editorial_fallback_provider", sa.String(), nullable=True)),
        ("provider_priority", sa.Column("provider_priority", sa.Text(), nullable=True)),
        ("approved_models", sa.Column("approved_models", sa.Text(), nullable=True)),
    ]:
        if not _has_column(table_name, column_name):
            op.add_column(table_name, column)


def downgrade() -> None:
    pass
