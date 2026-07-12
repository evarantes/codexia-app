"""add_video_task_idempotency_tables

Revision ID: 7f3c2e1a9b44
Revises: 560e9fa07258
Create Date: 2026-07-12 01:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# revision identifiers, used by Alembic.
revision: str = "7f3c2e1a9b44"
down_revision: Union[str, None] = "560e9fa07258"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return inspect(op.get_bind())


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table_name)}


def _index_names(inspector, table_name: str) -> set[str]:
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def _unique_constraint_names(inspector, table_name: str) -> set[str]:
    return {item["name"] for item in inspector.get_unique_constraints(table_name) if item.get("name")}


def _ensure_column(table_name: str, column: sa.Column) -> None:
    inspector = _inspector()
    if not _table_exists(inspector, table_name):
        return
    if column.name in _column_names(inspector, table_name):
        return
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.add_column(column)


def _assert_no_duplicates(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    row = bind.execute(
        text(
            f"""
            SELECT {column_name}
            FROM {table_name}
            WHERE {column_name} IS NOT NULL
            GROUP BY {column_name}
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).fetchone()
    if row:
        raise RuntimeError(
            f"Nao e seguro criar unicidade em {table_name}.{column_name}: existem valores duplicados."
        )


def _ensure_unique_constraint(table_name: str, constraint_name: str, columns: list[str]) -> None:
    inspector = _inspector()
    if not _table_exists(inspector, table_name):
        return
    if constraint_name in _unique_constraint_names(inspector, table_name):
        return
    for column_name in columns:
        _assert_no_duplicates(table_name, column_name)
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.create_unique_constraint(constraint_name, columns)


def _ensure_index(table_name: str, index_name: str, columns: list[str], unique: bool = False) -> None:
    inspector = _inspector()
    if not _table_exists(inspector, table_name):
        return
    if index_name in _index_names(inspector, table_name):
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    inspector = _inspector()
    if not _table_exists(inspector, table_name):
        return
    if index_name not in _index_names(inspector, table_name):
        return
    op.drop_index(index_name, table_name=table_name)


def _drop_unique_constraint_if_exists(table_name: str, constraint_name: str) -> None:
    inspector = _inspector()
    if not _table_exists(inspector, table_name):
        return
    if constraint_name not in _unique_constraint_names(inspector, table_name):
        return
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="unique")


def _create_video_task_dedupe_table() -> None:
    inspector = _inspector()
    if _table_exists(inspector, "video_task_dedupe"):
        return
    op.create_table(
        "video_task_dedupe",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_video_task_dedupe_idempotency_key"),
        sa.UniqueConstraint("task_id", name="uq_video_task_dedupe_task_id"),
    )


def _create_video_task_locks_table() -> None:
    inspector = _inspector()
    if _table_exists(inspector, "video_task_locks"):
        return
    op.create_table(
        "video_task_locks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("lock_key", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=True),
        sa.Column("acquired_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("task_id", name="uq_video_task_locks_task_id"),
        sa.UniqueConstraint("lock_key", name="uq_video_task_locks_lock_key"),
    )


def _create_video_task_leases_table() -> None:
    inspector = _inspector()
    if _table_exists(inspector, "video_task_leases"):
        return
    op.create_table(
        "video_task_leases",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=False),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("acquired_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("task_id", name="uq_video_task_leases_task_id"),
    )


def upgrade() -> None:
    _create_video_task_dedupe_table()
    _create_video_task_locks_table()
    _create_video_task_leases_table()

    _ensure_column("video_task_dedupe", sa.Column("request_fingerprint", sa.String(length=64), nullable=True))
    _ensure_column("video_task_dedupe", sa.Column("payload_json", sa.Text(), nullable=True))
    _ensure_column("video_task_dedupe", sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"))
    _ensure_column("video_task_dedupe", sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))
    _ensure_column("video_task_dedupe", sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))
    _ensure_column("video_task_dedupe", sa.Column("expires_at", sa.DateTime(), nullable=True))
    _ensure_unique_constraint("video_task_dedupe", "uq_video_task_dedupe_idempotency_key", ["idempotency_key"])
    _ensure_unique_constraint("video_task_dedupe", "uq_video_task_dedupe_task_id", ["task_id"])
    _ensure_index("video_task_dedupe", "ix_video_task_dedupe_expires_at", ["expires_at"])
    _ensure_index("video_task_dedupe", "ix_video_task_dedupe_status", ["status"])

    _ensure_column("video_task_locks", sa.Column("lock_key", sa.String(length=128), nullable=False))
    _ensure_column("video_task_locks", sa.Column("owner_id", sa.String(length=128), nullable=True))
    _ensure_column("video_task_locks", sa.Column("acquired_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))
    _ensure_column("video_task_locks", sa.Column("expires_at", sa.DateTime(), nullable=False))
    _ensure_column("video_task_locks", sa.Column("released_at", sa.DateTime(), nullable=True))
    _ensure_unique_constraint("video_task_locks", "uq_video_task_locks_task_id", ["task_id"])
    _ensure_unique_constraint("video_task_locks", "uq_video_task_locks_lock_key", ["lock_key"])
    _ensure_index("video_task_locks", "ix_video_task_locks_expires_at", ["expires_at"])
    _ensure_index("video_task_locks", "ix_video_task_locks_owner_id", ["owner_id"])

    _ensure_column("video_task_leases", sa.Column("lease_owner", sa.String(length=128), nullable=False))
    _ensure_column("video_task_leases", sa.Column("lease_token", sa.String(length=128), nullable=True))
    _ensure_column("video_task_leases", sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"))
    _ensure_column("video_task_leases", sa.Column("acquired_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))
    _ensure_column("video_task_leases", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    _ensure_column("video_task_leases", sa.Column("expires_at", sa.DateTime(), nullable=False))
    _ensure_column("video_task_leases", sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))
    _ensure_column("video_task_leases", sa.Column("last_error", sa.Text(), nullable=True))
    _ensure_unique_constraint("video_task_leases", "uq_video_task_leases_task_id", ["task_id"])
    _ensure_index("video_task_leases", "ix_video_task_leases_expires_at", ["expires_at"])
    _ensure_index("video_task_leases", "ix_video_task_leases_owner_attempt", ["lease_owner", "attempt_number"])


def downgrade() -> None:
    _drop_index_if_exists("video_task_leases", "ix_video_task_leases_owner_attempt")
    _drop_index_if_exists("video_task_leases", "ix_video_task_leases_expires_at")
    _drop_unique_constraint_if_exists("video_task_leases", "uq_video_task_leases_task_id")

    _drop_index_if_exists("video_task_locks", "ix_video_task_locks_owner_id")
    _drop_index_if_exists("video_task_locks", "ix_video_task_locks_expires_at")
    _drop_unique_constraint_if_exists("video_task_locks", "uq_video_task_locks_lock_key")
    _drop_unique_constraint_if_exists("video_task_locks", "uq_video_task_locks_task_id")

    _drop_index_if_exists("video_task_dedupe", "ix_video_task_dedupe_status")
    _drop_index_if_exists("video_task_dedupe", "ix_video_task_dedupe_expires_at")
    _drop_unique_constraint_if_exists("video_task_dedupe", "uq_video_task_dedupe_task_id")
    _drop_unique_constraint_if_exists("video_task_dedupe", "uq_video_task_dedupe_idempotency_key")

    inspector = _inspector()
    if _table_exists(inspector, "video_task_leases"):
        op.drop_table("video_task_leases")
    inspector = _inspector()
    if _table_exists(inspector, "video_task_locks"):
        op.drop_table("video_task_locks")
    inspector = _inspector()
    if _table_exists(inspector, "video_task_dedupe"):
        op.drop_table("video_task_dedupe")
