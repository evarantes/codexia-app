"""add_ai_router_settings_tables

Revision ID: 9c2d7a1b3f4e
Revises: b8f4a7c9d321
Create Date: 2026-07-28 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c2d7a1b3f4e"
down_revision: Union[str, None] = "b8f4a7c9d321"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    settings_table = "settings"
    if _has_table(settings_table):
        for col_name, ddl in [
            ("openai_image_model", "ALTER TABLE settings ADD COLUMN openai_image_model VARCHAR NULL"),
            ("openai_allow_text", "ALTER TABLE settings ADD COLUMN openai_allow_text BOOLEAN NOT NULL DEFAULT FALSE"),
            ("openai_allow_script", "ALTER TABLE settings ADD COLUMN openai_allow_script BOOLEAN NOT NULL DEFAULT FALSE"),
            ("openai_allow_editorial", "ALTER TABLE settings ADD COLUMN openai_allow_editorial BOOLEAN NOT NULL DEFAULT FALSE"),
            ("openai_allow_analysis", "ALTER TABLE settings ADD COLUMN openai_allow_analysis BOOLEAN NOT NULL DEFAULT FALSE"),
            ("openai_allow_images", "ALTER TABLE settings ADD COLUMN openai_allow_images BOOLEAN NOT NULL DEFAULT TRUE"),
            ("openai_allow_thumbnail", "ALTER TABLE settings ADD COLUMN openai_allow_thumbnail BOOLEAN NOT NULL DEFAULT TRUE"),
            ("openai_allow_transcription", "ALTER TABLE settings ADD COLUMN openai_allow_transcription BOOLEAN NOT NULL DEFAULT FALSE"),
            ("openai_allow_tts", "ALTER TABLE settings ADD COLUMN openai_allow_tts BOOLEAN NOT NULL DEFAULT FALSE"),
            ("openai_allow_embeddings", "ALTER TABLE settings ADD COLUMN openai_allow_embeddings BOOLEAN NOT NULL DEFAULT FALSE"),
            ("openai_allow_other", "ALTER TABLE settings ADD COLUMN openai_allow_other BOOLEAN NOT NULL DEFAULT FALSE"),
            ("ai_cb_failure_threshold", "ALTER TABLE settings ADD COLUMN ai_cb_failure_threshold INTEGER NULL"),
            ("ai_cb_cooldown_seconds", "ALTER TABLE settings ADD COLUMN ai_cb_cooldown_seconds INTEGER NULL"),
            ("ai_cb_half_open_max_attempts", "ALTER TABLE settings ADD COLUMN ai_cb_half_open_max_attempts INTEGER NULL"),
            ("gemini_script_model", "ALTER TABLE settings ADD COLUMN gemini_script_model VARCHAR NULL"),
            ("gemini_text_model", "ALTER TABLE settings ADD COLUMN gemini_text_model VARCHAR NULL"),
            ("gemini_editorial_model", "ALTER TABLE settings ADD COLUMN gemini_editorial_model VARCHAR NULL"),
            ("gemini_analysis_model", "ALTER TABLE settings ADD COLUMN gemini_analysis_model VARCHAR NULL"),
            ("groq_transcription_model", "ALTER TABLE settings ADD COLUMN groq_transcription_model VARCHAR NULL"),
            ("groq_text_model", "ALTER TABLE settings ADD COLUMN groq_text_model VARCHAR NULL"),
        ]:
            if not _has_column(settings_table, col_name):
                op.execute(ddl)

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_capability_policies (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NULL,
            capability VARCHAR(64) NOT NULL,
            primary_provider VARCHAR(64) NOT NULL,
            primary_model VARCHAR(128) NULL,
            fallback_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            fallback_provider VARCHAR(64) NULL,
            fallback_model VARCHAR(128) NULL,
            cache_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            estimated_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
            max_cost DOUBLE PRECISION NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_capability_policies_user_capability
        ON ai_capability_policies (COALESCE(user_id, 0), capability)
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_capability_policies_capability ON ai_capability_policies (capability)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_capability_policies_user_id ON ai_capability_policies (user_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_operation_cache (
            cache_key VARCHAR(128) PRIMARY KEY,
            capability VARCHAR(64) NOT NULL,
            provider VARCHAR(64) NOT NULL,
            model VARCHAR(128) NOT NULL,
            input_hash VARCHAR(64) NOT NULL,
            parameters_version VARCHAR(32) NOT NULL,
            response_json TEXT NOT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMP WITHOUT TIME ZONE NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_operation_cache_capability ON ai_operation_cache (capability)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_operation_cache_input_hash ON ai_operation_cache (input_hash)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_operation_cache_expires_at ON ai_operation_cache (expires_at)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_operation_runs (
            operation_id VARCHAR(64) PRIMARY KEY,
            user_id INTEGER NULL,
            scope_type VARCHAR(32) NOT NULL DEFAULT 'global',
            scope_id VARCHAR(64) NULL,
            capability VARCHAR(64) NOT NULL,
            provider VARCHAR(64) NOT NULL,
            model VARCHAR(128) NOT NULL,
            input_hash VARCHAR(64) NOT NULL,
            parameters_version VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'running',
            result_json TEXT NULL,
            error_json TEXT NULL,
            estimated_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
            actual_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
            latency_ms INTEGER NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMP WITHOUT TIME ZONE NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_operation_runs_scope ON ai_operation_runs (scope_type, scope_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_operation_runs_capability ON ai_operation_runs (capability)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_operation_runs_updated_at ON ai_operation_runs (updated_at)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_provider_circuit_breakers (
            provider VARCHAR(64) PRIMARY KEY,
            state VARCHAR(16) NOT NULL DEFAULT 'closed',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            opened_at TIMESTAMP WITHOUT TIME ZONE NULL,
            last_failure_at TIMESTAMP WITHOUT TIME ZONE NULL,
            last_success_at TIMESTAMP WITHOUT TIME ZONE NULL,
            cooldown_until TIMESTAMP WITHOUT TIME ZONE NULL,
            half_open_remaining INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_provider_cb_state ON ai_provider_circuit_breakers (state)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_provider_cb_updated_at ON ai_provider_circuit_breakers (updated_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ai_provider_cb_updated_at")
    op.execute("DROP INDEX IF EXISTS idx_ai_provider_cb_state")
    op.execute("DROP TABLE IF EXISTS ai_provider_circuit_breakers")

    op.execute("DROP INDEX IF EXISTS idx_ai_operation_runs_updated_at")
    op.execute("DROP INDEX IF EXISTS idx_ai_operation_runs_capability")
    op.execute("DROP INDEX IF EXISTS idx_ai_operation_runs_scope")
    op.execute("DROP TABLE IF EXISTS ai_operation_runs")

    op.execute("DROP INDEX IF EXISTS idx_ai_operation_cache_expires_at")
    op.execute("DROP INDEX IF EXISTS idx_ai_operation_cache_input_hash")
    op.execute("DROP INDEX IF EXISTS idx_ai_operation_cache_capability")
    op.execute("DROP TABLE IF EXISTS ai_operation_cache")

    op.execute("DROP INDEX IF EXISTS idx_ai_capability_policies_user_id")
    op.execute("DROP INDEX IF EXISTS idx_ai_capability_policies_capability")
    op.execute("DROP INDEX IF EXISTS uq_ai_capability_policies_user_capability")
    op.execute("DROP TABLE IF EXISTS ai_capability_policies")
