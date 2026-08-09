"""merge story pipeline schema heads

Consolida o Alembic do História/Devocional (Texto -> Vídeo) como a
linhagem canônica do sistema sem reescrever migrations já publicadas.

``a9b2c4d6e8f0`` cria ``unified_videos``. A migration de reconciliação
``f8a7b2c4d6e0`` nasceu em paralelo e, dependendo da ordem escolhida pelo
Alembic, podia tentar reconciliar essa tabela antes de ela existir. Este
merge fecha os dois heads e reaplica, de forma idempotente e não destrutiva,
as colunas do contrato unificado que poderiam ter sido ignoradas.

Revision ID: b1c2d3e4f5a6
Revises: a9b2c4d6e8f0, f8a7b2c4d6e0
Create Date: 2026-08-09 16:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = (
    "a9b2c4d6e8f0",
    "f8a7b2c4d6e0",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "unified_videos"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in (inspector.get_table_names() or []):
        raise RuntimeError(
            "A tabela unified_videos não existe após a linhagem canônica "
            "a9b2c4d6e8f0; o banco não pode ser marcado como atualizado."
        )
    return {str(column.get("name") or "") for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    existing = _existing_columns()
    canonical_columns = [
        sa.Column(
            "force_reuse_assets",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "force_render_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("review_feedback_json", sa.Text(), nullable=True),
        sa.Column("render_logs_json", sa.Text(), nullable=True),
    ]
    for column in canonical_columns:
        if str(column.name) not in existing:
            op.add_column(_TABLE, column)
            existing.add(str(column.name))

    inspector = sa.inspect(op.get_bind())
    if "scheduled_videos" in (inspector.get_table_names() or []):
        scheduled_columns = {
            str(column.get("name") or "")
            for column in inspector.get_columns("scheduled_videos")
        }
        required_links = {"task_id", "unified_video_id"}
        missing_links = sorted(required_links - scheduled_columns)
        if missing_links:
            raise RuntimeError(
                "A reconciliação f8a7b2c4d6e0 não criou os vínculos canônicos "
                f"em scheduled_videos: {', '.join(missing_links)}"
            )
        existing_indexes = {
            str(index.get("name") or "")
            for index in inspector.get_indexes("scheduled_videos")
        }
        for index_name, column_name in [
            ("ix_scheduled_videos_task_id", "task_id"),
            ("ix_scheduled_videos_unified_video_id", "unified_video_id"),
        ]:
            if index_name not in existing_indexes:
                op.create_index(index_name, "scheduled_videos", [column_name], unique=False)

    inspector = sa.inspect(op.get_bind())
    humor_table = "codexia_humor_projects"
    if humor_table in (inspector.get_table_names() or []):
        humor_columns = {
            str(column.get("name") or "")
            for column in inspector.get_columns(humor_table)
        }
        for column in [
            sa.Column("task_id", sa.String(length=64), nullable=True),
            sa.Column("unified_video_id", sa.Integer(), nullable=True),
            sa.Column("pipeline", sa.String(length=64), nullable=True),
        ]:
            if str(column.name) not in humor_columns:
                op.add_column(humor_table, column)
        humor_indexes = {
            str(index.get("name") or "")
            for index in sa.inspect(op.get_bind()).get_indexes(humor_table)
        }
        for index_name, column_name in [
            ("ix_codexia_humor_projects_task_id", "task_id"),
            ("ix_codexia_humor_projects_unified_video_id", "unified_video_id"),
        ]:
            if index_name not in humor_indexes:
                op.create_index(index_name, humor_table, [column_name], unique=False)

    inspector = sa.inspect(op.get_bind())
    production_table = "videos"
    if production_table in (inspector.get_table_names() or []):
        production_columns = {
            str(column.get("name") or "")
            for column in inspector.get_columns(production_table)
        }
        for column in [
            sa.Column("task_id", sa.String(length=64), nullable=True),
            sa.Column("unified_video_id", sa.Integer(), nullable=True),
            sa.Column("pipeline", sa.String(length=64), nullable=True),
        ]:
            if str(column.name) not in production_columns:
                op.add_column(production_table, column)
        production_indexes = {
            str(index.get("name") or "")
            for index in sa.inspect(op.get_bind()).get_indexes(production_table)
        }
        for index_name, column_name in [
            ("ix_videos_task_id", "task_id"),
            ("ix_videos_unified_video_id", "unified_video_id"),
        ]:
            if index_name not in production_indexes:
                op.create_index(index_name, production_table, [column_name], unique=False)


def downgrade() -> None:
    # Merge de histórico: não removemos colunas nem dados compartilhados.
    # O downgrade volta apenas o ponteiro do Alembic para os dois pais.
    pass
