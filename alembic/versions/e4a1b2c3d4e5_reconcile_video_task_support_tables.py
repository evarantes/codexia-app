"""reconcile_video_task_support_tables

Revision ID: e4a1b2c3d4e5
Revises: c60c7aca98ee
Create Date: 2026-08-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4a1b2c3d4e5"
down_revision: Union[str, None] = "c60c7aca98ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TASK_DEDUPE_TABLE = "video_task_dedupe"
TASK_LEASE_TABLE = "video_task_leases"
TASK_LOCK_TABLE = "video_task_locks"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in (_inspector().get_table_names() or [])


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TASK_DEDUPE_TABLE} (
            idempotency_key VARCHAR(255) PRIMARY KEY,
            request_hash TEXT NOT NULL,
            task_id VARCHAR(64) NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            request_payload_json TEXT NULL,
            result_json TEXT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMP WITHOUT TIME ZONE NULL,
            completed_at TIMESTAMP WITHOUT TIME ZONE NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TASK_LEASE_TABLE} (
            task_id VARCHAR(64) PRIMARY KEY,
            executor_id VARCHAR(255) NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            heartbeat_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            lease_expires_at TIMESTAMP WITHOUT TIME ZONE NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TASK_LOCK_TABLE} (
            lock_key VARCHAR(255) PRIMARY KEY,
            owner_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_DEDUPE_TABLE}_task_id ON {TASK_DEDUPE_TABLE} (task_id)")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{TASK_DEDUPE_TABLE}_task_id_not_null ON {TASK_DEDUPE_TABLE} (task_id) WHERE task_id IS NOT NULL")
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_DEDUPE_TABLE}_status ON {TASK_DEDUPE_TABLE} (status)")
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_DEDUPE_TABLE}_request_hash ON {TASK_DEDUPE_TABLE} (request_hash)")
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_DEDUPE_TABLE}_updated_at ON {TASK_DEDUPE_TABLE} (updated_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_DEDUPE_TABLE}_expires_at ON {TASK_DEDUPE_TABLE} (expires_at)")

    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_LEASE_TABLE}_executor_id ON {TASK_LEASE_TABLE} (executor_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_LEASE_TABLE}_updated_at ON {TASK_LEASE_TABLE} (updated_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_LEASE_TABLE}_expires_at ON {TASK_LEASE_TABLE} (expires_at)")

    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_LOCK_TABLE}_updated_at ON {TASK_LOCK_TABLE} (updated_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{TASK_LOCK_TABLE}_expires_at ON {TASK_LOCK_TABLE} (expires_at)")


def downgrade() -> None:
    if _has_table(TASK_LOCK_TABLE):
        op.execute(f"DROP TABLE IF EXISTS {TASK_LOCK_TABLE}")
    if _has_table(TASK_LEASE_TABLE):
        op.execute(f"DROP TABLE IF EXISTS {TASK_LEASE_TABLE}")
    if _has_table(TASK_DEDUPE_TABLE):
        op.execute(f"DROP TABLE IF EXISTS {TASK_DEDUPE_TABLE}")

