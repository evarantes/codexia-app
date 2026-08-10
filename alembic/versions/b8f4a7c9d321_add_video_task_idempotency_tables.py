"""add_video_task_idempotency_tables

Revision ID: b8f4a7c9d321
Revises: 560e9fa07258
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8f4a7c9d321"
down_revision: Union[str, None] = "560e9fa07258"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TASK_DEDUPE_TABLE = "video_task_dedupe"
TASK_LEASE_TABLE = "video_task_leases"
TASK_LOCK_TABLE = "video_task_locks"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    try:
        return column_name in {str(col.get("name") or "") for col in _inspector().get_columns(table_name)}
    except Exception:
        return False


def upgrade() -> None:
    dialect = str(getattr(op.get_bind().dialect, "name", "") or "").lower()
    if dialect == "sqlite":
        # SQLite existe apenas no desenvolvimento local. Mantemos o mesmo
        # contrato de colunas com tipos/defaults portáveis e deixamos a
        # reconciliação ALTER COLUMN abaixo exclusivamente para PostgreSQL.
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TASK_DEDUPE_TABLE} (
                idempotency_key VARCHAR(255) PRIMARY KEY,
                request_hash TEXT NOT NULL,
                task_id VARCHAR(64) NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                request_payload_json TEXT NULL,
                result_json TEXT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME NULL,
                completed_at DATETIME NULL
            )
            """
        )
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TASK_LEASE_TABLE} (
                task_id VARCHAR(64) PRIMARY KEY,
                executor_id VARCHAR(255) NOT NULL,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                heartbeat_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                lease_expires_at DATETIME NULL
            )
            """
        )
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TASK_LOCK_TABLE} (
                lock_key VARCHAR(255) PRIMARY KEY,
                owner_id VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        return

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

    if _has_table(TASK_DEDUPE_TABLE):
        if not _has_column(TASK_DEDUPE_TABLE, "updated_at"):
            op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ADD COLUMN updated_at TIMESTAMP WITHOUT TIME ZONE NULL")
        if not _has_column(TASK_DEDUPE_TABLE, "expires_at"):
            op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ADD COLUMN expires_at TIMESTAMP WITHOUT TIME ZONE NULL")
        if not _has_column(TASK_DEDUPE_TABLE, "completed_at"):
            op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ADD COLUMN completed_at TIMESTAMP WITHOUT TIME ZONE NULL")
        op.execute(
            f"""
            UPDATE {TASK_DEDUPE_TABLE}
            SET updated_at = COALESCE(updated_at, created_at, NOW()),
                expires_at = COALESCE(expires_at, completed_at, updated_at, created_at, NOW())
            """
        )
        op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ALTER COLUMN created_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ALTER COLUMN updated_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ALTER COLUMN request_hash SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ALTER COLUMN status SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ALTER COLUMN created_at SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_DEDUPE_TABLE} ALTER COLUMN updated_at SET NOT NULL")

    if _has_table(TASK_LEASE_TABLE):
        if not _has_column(TASK_LEASE_TABLE, "created_at"):
            op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ADD COLUMN created_at TIMESTAMP WITHOUT TIME ZONE NULL")
        if not _has_column(TASK_LEASE_TABLE, "updated_at"):
            op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ADD COLUMN updated_at TIMESTAMP WITHOUT TIME ZONE NULL")
        if not _has_column(TASK_LEASE_TABLE, "expires_at"):
            op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ADD COLUMN expires_at TIMESTAMP WITHOUT TIME ZONE NULL")
        if not _has_column(TASK_LEASE_TABLE, "lease_expires_at"):
            op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ADD COLUMN lease_expires_at TIMESTAMP WITHOUT TIME ZONE NULL")
        op.execute(
            f"""
            UPDATE {TASK_LEASE_TABLE}
            SET created_at = COALESCE(created_at, started_at, heartbeat_at, lease_expires_at, NOW()),
                updated_at = COALESCE(updated_at, heartbeat_at, started_at, created_at, NOW()),
                expires_at = COALESCE(expires_at, lease_expires_at, heartbeat_at, started_at, NOW()),
                lease_expires_at = COALESCE(lease_expires_at, expires_at, heartbeat_at, started_at, NOW())
            """
        )
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN executor_id SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN attempt_number SET DEFAULT 1")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN attempt_number SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN created_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN updated_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN started_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN heartbeat_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN expires_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN created_at SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN updated_at SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN started_at SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN heartbeat_at SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LEASE_TABLE} ALTER COLUMN expires_at SET NOT NULL")

    if _has_table(TASK_LOCK_TABLE):
        if not _has_column(TASK_LOCK_TABLE, "created_at"):
            op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ADD COLUMN created_at TIMESTAMP WITHOUT TIME ZONE NULL")
        if not _has_column(TASK_LOCK_TABLE, "updated_at"):
            op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ADD COLUMN updated_at TIMESTAMP WITHOUT TIME ZONE NULL")
        if not _has_column(TASK_LOCK_TABLE, "expires_at"):
            op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ADD COLUMN expires_at TIMESTAMP WITHOUT TIME ZONE NULL")
        op.execute(
            f"""
            UPDATE {TASK_LOCK_TABLE}
            SET created_at = COALESCE(created_at, updated_at, expires_at, NOW()),
                updated_at = COALESCE(updated_at, created_at, expires_at, NOW()),
                expires_at = COALESCE(expires_at, updated_at, created_at, NOW())
            """
        )
        op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ALTER COLUMN owner_id SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ALTER COLUMN created_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ALTER COLUMN updated_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ALTER COLUMN expires_at SET DEFAULT NOW()")
        op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ALTER COLUMN created_at SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ALTER COLUMN updated_at SET NOT NULL")
        op.execute(f"ALTER TABLE {TASK_LOCK_TABLE} ALTER COLUMN expires_at SET NOT NULL")

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
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_LOCK_TABLE}_expires_at")
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_LOCK_TABLE}_updated_at")

    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_LEASE_TABLE}_expires_at")
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_LEASE_TABLE}_updated_at")
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_LEASE_TABLE}_executor_id")

    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_DEDUPE_TABLE}_expires_at")
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_DEDUPE_TABLE}_updated_at")
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_DEDUPE_TABLE}_request_hash")
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_DEDUPE_TABLE}_status")
    op.execute(f"DROP INDEX IF EXISTS uq_{TASK_DEDUPE_TABLE}_task_id_not_null")
    op.execute(f"DROP INDEX IF EXISTS idx_{TASK_DEDUPE_TABLE}_task_id")

    op.execute(f"DROP TABLE IF EXISTS {TASK_LOCK_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {TASK_LEASE_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {TASK_DEDUPE_TABLE}")
