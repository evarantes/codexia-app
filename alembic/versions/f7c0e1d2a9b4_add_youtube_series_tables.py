"""add_youtube_series_tables

Revision ID: f7c0e1d2a9b4
Revises: c1f9e6a4b2d3
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f7c0e1d2a9b4"
down_revision: Union[str, None] = "c1f9e6a4b2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    try:
        return table_name in _inspector().get_table_names()
    except Exception:
        return False


def _has_index(table_name: str, index_name: str) -> bool:
    try:
        return index_name in {str(idx.get("name") or "") for idx in _inspector().get_indexes(table_name)}
    except Exception:
        return False


def _create_index_if_missing(index_name: str, table_name: str, columns, unique: bool = False) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    if not _has_table("series_plans"):
        op.create_table(
            "series_plans",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.String(), nullable=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("main_theme", sa.String(), nullable=False),
            sa.Column("objective", sa.Text(), nullable=True),
            sa.Column("target_audience", sa.String(), nullable=True),
            sa.Column("content_type", sa.String(), nullable=False, server_default="reflection"),
            sa.Column("start_date", sa.DateTime(), nullable=False),
            sa.Column("end_date", sa.DateTime(), nullable=False),
            sa.Column("publication_time", sa.String(), nullable=False, server_default="19:00"),
            sa.Column("timezone", sa.String(), nullable=False, server_default="UTC"),
            sa.Column("production_lead_days", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("production_time", sa.String(), nullable=False, server_default="06:00"),
            sa.Column("duration_minutes", sa.Integer(), nullable=True),
            sa.Column("visibility", sa.String(), nullable=False, server_default="unlisted"),
            sa.Column("tone", sa.String(), nullable=True),
            sa.Column("narration_style", sa.String(), nullable=True),
            sa.Column("continuity_level", sa.String(), nullable=True),
            sa.Column("hook_intensity", sa.String(), nullable=True),
            sa.Column("use_biblical_references", sa.Boolean(), nullable=True, server_default=sa.text("true")),
            sa.Column("cta_subscribe", sa.Boolean(), nullable=True, server_default=sa.text("true")),
            sa.Column("cta_next_episode", sa.Boolean(), nullable=True, server_default=sa.text("true")),
            sa.Column("auto_approval", sa.Boolean(), nullable=True, server_default=sa.text("false")),
            sa.Column("status", sa.String(), nullable=False, server_default="draft"),
            sa.Column("total_episodes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("current_episode", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("editorial_plan_json", sa.Text(), nullable=True),
            sa.Column("editorial_memory_json", sa.Text(), nullable=True),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_if_missing(op.f("ix_series_plans_id"), "series_plans", ["id"])
    _create_index_if_missing(op.f("ix_series_plans_user_id"), "series_plans", ["user_id"])
    _create_index_if_missing(op.f("ix_series_plans_channel_id"), "series_plans", ["channel_id"])
    _create_index_if_missing(op.f("ix_series_plans_name"), "series_plans", ["name"])
    _create_index_if_missing(op.f("ix_series_plans_main_theme"), "series_plans", ["main_theme"])
    _create_index_if_missing(op.f("ix_series_plans_status"), "series_plans", ["status"])

    if not _has_table("series_episodes"):
        op.create_table(
            "series_episodes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("series_id", sa.Integer(), nullable=False),
            sa.Column("episode_number", sa.Integer(), nullable=False),
            sa.Column("planned_title", sa.String(), nullable=False),
            sa.Column("narrated_title", sa.String(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("previous_episode_hook", sa.Text(), nullable=True),
            sa.Column("next_episode_hook", sa.Text(), nullable=True),
            sa.Column("publication_datetime", sa.DateTime(), nullable=False),
            sa.Column("production_datetime", sa.DateTime(), nullable=False),
            sa.Column("duration_minutes", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="planned"),
            sa.Column("task_id", sa.String(), nullable=True),
            sa.Column("scheduled_video_id", sa.Integer(), nullable=True),
            sa.Column("content_fingerprint", sa.String(), nullable=True),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
            sa.Column("approved_by", sa.Integer(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("youtube_video_id", sa.String(), nullable=True),
            sa.Column("youtube_url", sa.String(), nullable=True),
            sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("correction_plan_json", sa.Text(), nullable=True),
            sa.Column("approved_snapshot_json", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["scheduled_video_id"], ["scheduled_videos.id"]),
            sa.ForeignKeyConstraint(["series_id"], ["series_plans.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["task_id"], ["video_tasks.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("series_id", "episode_number", name="uq_series_episodes_series_episode_number"),
        )
    _create_index_if_missing(op.f("ix_series_episodes_id"), "series_episodes", ["id"])
    _create_index_if_missing(op.f("ix_series_episodes_series_id"), "series_episodes", ["series_id"])
    _create_index_if_missing(op.f("ix_series_episodes_episode_number"), "series_episodes", ["episode_number"])
    _create_index_if_missing(op.f("ix_series_episodes_publication_datetime"), "series_episodes", ["publication_datetime"])
    _create_index_if_missing(op.f("ix_series_episodes_production_datetime"), "series_episodes", ["production_datetime"])
    _create_index_if_missing(op.f("ix_series_episodes_status"), "series_episodes", ["status"])
    _create_index_if_missing(op.f("ix_series_episodes_task_id"), "series_episodes", ["task_id"])
    _create_index_if_missing(op.f("ix_series_episodes_scheduled_video_id"), "series_episodes", ["scheduled_video_id"])
    _create_index_if_missing(op.f("ix_series_episodes_content_fingerprint"), "series_episodes", ["content_fingerprint"])

    if not _has_table("episode_reviews"):
        op.create_table(
            "episode_reviews",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("episode_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("decision", sa.String(), nullable=False),
            sa.Column("reason_categories", sa.Text(), nullable=True),
            sa.Column("feedback", sa.Text(), nullable=True),
            sa.Column("affected_components", sa.Text(), nullable=True),
            sa.Column("reused_components", sa.Text(), nullable=True),
            sa.Column("regenerated_components", sa.Text(), nullable=True),
            sa.Column("estimated_cost", sa.Float(), nullable=True),
            sa.Column("actual_cost", sa.Float(), nullable=True),
            sa.Column("result_summary", sa.Text(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("reviewed_by", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["episode_id"], ["series_episodes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_if_missing(op.f("ix_episode_reviews_id"), "episode_reviews", ["id"])
    _create_index_if_missing(op.f("ix_episode_reviews_episode_id"), "episode_reviews", ["episode_id"])
    _create_index_if_missing(op.f("ix_episode_reviews_decision"), "episode_reviews", ["decision"])


def downgrade() -> None:
    op.drop_index(op.f("ix_episode_reviews_decision"), table_name="episode_reviews")
    op.drop_index(op.f("ix_episode_reviews_episode_id"), table_name="episode_reviews")
    op.drop_index(op.f("ix_episode_reviews_id"), table_name="episode_reviews")
    op.drop_table("episode_reviews")

    op.drop_index(op.f("ix_series_episodes_content_fingerprint"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_scheduled_video_id"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_task_id"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_status"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_production_datetime"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_publication_datetime"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_episode_number"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_series_id"), table_name="series_episodes")
    op.drop_index(op.f("ix_series_episodes_id"), table_name="series_episodes")
    op.drop_table("series_episodes")

    op.drop_index(op.f("ix_series_plans_status"), table_name="series_plans")
    op.drop_index(op.f("ix_series_plans_main_theme"), table_name="series_plans")
    op.drop_index(op.f("ix_series_plans_name"), table_name="series_plans")
    op.drop_index(op.f("ix_series_plans_channel_id"), table_name="series_plans")
    op.drop_index(op.f("ix_series_plans_user_id"), table_name="series_plans")
    op.drop_index(op.f("ix_series_plans_id"), table_name="series_plans")
    op.drop_table("series_plans")
