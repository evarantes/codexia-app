"""add_youtube_auto_audit_events

Revision ID: ab12cd34ef56
Revises: e4a1b2c3d4e5
Create Date: 2026-08-02 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


revision: str = "ab12cd34ef56"
down_revision: Union[str, None] = "e4a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_auto_audit_events (
            id SERIAL PRIMARY KEY,
            event_type VARCHAR(255) NOT NULL,
            series_id INTEGER NULL REFERENCES series_plans(id),
            episode_id INTEGER NULL REFERENCES series_episodes(id),
            task_id VARCHAR(255) NULL,
            scheduled_video_id INTEGER NULL REFERENCES scheduled_videos(id),
            status_before VARCHAR(255) NULL,
            status_after VARCHAR(255) NULL,
            duration_ms INTEGER NULL,
            payload_json TEXT NULL,
            error_stack TEXT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_youtube_auto_audit_events_event_type ON youtube_auto_audit_events (event_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_youtube_auto_audit_events_series_id ON youtube_auto_audit_events (series_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_youtube_auto_audit_events_episode_id ON youtube_auto_audit_events (episode_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_youtube_auto_audit_events_task_id ON youtube_auto_audit_events (task_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_youtube_auto_audit_events_scheduled_video_id ON youtube_auto_audit_events (scheduled_video_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_youtube_auto_audit_events_created_at ON youtube_auto_audit_events (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS youtube_auto_audit_events")

