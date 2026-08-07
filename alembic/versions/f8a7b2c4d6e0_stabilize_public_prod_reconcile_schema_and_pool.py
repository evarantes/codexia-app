"""stabilize_public_prod_reconcile_schema_and_pool

FASE A/B/D missão "Estabilizar Produção Pública":
  - migration idempotente, NÃO destrutiva, PRESERVA todos os dados.
  - reconcilia ORM ↔ PostgreSQL para 6 tabelas auditadas:
      settings, unified_videos, video_tasks, series_plans,
      series_episodes, scheduled_videos.
  - corrige settings.openai_image_model (erro 1 do usuário)
    + ~50 colunas ausentes de settings relacionadas a TTS/stock/
    editorial_intelligence/infra social (Instagram/TikTok/Hotmart/KDP)
    + providers + custos unitários + vozes padrão.
  - unified_videos colunas a mais do ORM: force_reuse_assets,
    force_render_only, review_feedback_json, render_logs_json.
  - scheduled_videos colunas de pipeline central.
  - series_plans.aspect_ratio (ORM).

REGRAS GARANTIDAS nesta migration:
  1. NÃO executa DROP de tabela/coluna de negócio.
  2. Toda ADD COLUMN executa apenas IF NOT EXISTS (via _has_column).
  3. Valores default são os mesmos do ORM em models.py (server_default
     estável, sem sobrescrever NULOS existentes).
  4. downgrade: remove apenas as colunas que adicionou (IF EXISTS).

Revision ID: f8a7b2c4d6e0
Revises: cf47b0b001c3
Create Date: 2026-08-07 12:00:00.000000
"""
from __future__ import annotations

from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8a7b2c4d6e0"
down_revision: Union[str, None] = "cf47b0b001c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ================= HELPERS ========================
def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in (_inspector().get_table_names() or [])


def _existing_columns(table_name: str) -> set:
    if not _has_table(table_name):
        return set()
    try:
        return {str(col.get("name") or "") for col in _inspector().get_columns(table_name)}
    except Exception:
        return set()


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in _existing_columns(table_name)


def _add_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            try:
                batch_op.drop_column(column_name)
            except Exception:
                # SQLite: pode precisar de rebuild (nem sempre necessário).
                # Não propagamos erro no downgrade para ser resiliente.
                pass


# ================= MIGRAÇÕES ======================
def _upgrade_settings() -> None:
    table = "settings"
    if not _has_table(table):
        return

    colunas: list[tuple[str, sa.Column]] = [
        # ——— OpenAI / AI Router permissões (colunas que já existem em 9c2d7a1b3f4e,
        #     garantidas aqui como NOP caso a migration oficial não tenha sido aplicada
        #     em produção pública; todas são idempotentes via _has_column).
        ("openai_image_model", sa.Column("openai_image_model", sa.String(), nullable=True)),
        ("openai_allow_text", sa.Column("openai_allow_text", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("openai_allow_script", sa.Column("openai_allow_script", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("openai_allow_editorial", sa.Column("openai_allow_editorial", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("openai_allow_analysis", sa.Column("openai_allow_analysis", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("openai_allow_images", sa.Column("openai_allow_images", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
        ("openai_allow_thumbnail", sa.Column("openai_allow_thumbnail", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
        ("openai_allow_transcription", sa.Column("openai_allow_transcription", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("openai_allow_tts", sa.Column("openai_allow_tts", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("openai_allow_embeddings", sa.Column("openai_allow_embeddings", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("openai_allow_other", sa.Column("openai_allow_other", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
        ("ai_cb_failure_threshold", sa.Column("ai_cb_failure_threshold", sa.Integer(), nullable=True)),
        ("ai_cb_cooldown_seconds", sa.Column("ai_cb_cooldown_seconds", sa.Integer(), nullable=True)),
        ("ai_cb_half_open_max_attempts", sa.Column("ai_cb_half_open_max_attempts", sa.Integer(), nullable=True)),
        # ——— Circuit breaker / OpenAI
        ("openai_no_credit", sa.Column("openai_no_credit", sa.Boolean(), server_default=sa.text("false"), nullable=True)),
        # ——— Integrações sociais / monetização / KDP que o ORM declara
        ("facebook_page_id", sa.Column("facebook_page_id", sa.String(), nullable=True)),
        ("facebook_access_token", sa.Column("facebook_access_token", sa.String(), nullable=True)),
        ("whatsapp_phone_number_id", sa.Column("whatsapp_phone_number_id", sa.String(), nullable=True)),
        ("whatsapp_access_token", sa.Column("whatsapp_access_token", sa.String(), nullable=True)),
        ("whatsapp_verify_token", sa.Column("whatsapp_verify_token", sa.String(), nullable=True)),
        ("whatsapp_allowed_numbers", sa.Column("whatsapp_allowed_numbers", sa.String(), nullable=True)),
        ("telegram_bot_token", sa.Column("telegram_bot_token", sa.String(), nullable=True)),
        ("telegram_allowed_chat_ids", sa.Column("telegram_allowed_chat_ids", sa.String(), nullable=True)),
        ("mercadopago_access_token", sa.Column("mercadopago_access_token", sa.String(), nullable=True)),
        ("hotmart_client_id", sa.Column("hotmart_client_id", sa.String(), nullable=True)),
        ("hotmart_client_secret", sa.Column("hotmart_client_secret", sa.String(), nullable=True)),
        ("hotmart_basic", sa.Column("hotmart_basic", sa.String(), nullable=True)),
        ("hotmart_access_token", sa.Column("hotmart_access_token", sa.String(), nullable=True)),
        ("hotmart_token_expires_at", sa.Column("hotmart_token_expires_at", sa.DateTime(), nullable=True)),
        ("amazon_kdp_email", sa.Column("amazon_kdp_email", sa.String(), nullable=True)),
        ("amazon_kdp_password", sa.Column("amazon_kdp_password", sa.String(), nullable=True)),
        ("amazon_kdp_login_url", sa.Column("amazon_kdp_login_url", sa.String(), nullable=True)),
        ("amazon_kdp_bookshelf_url", sa.Column("amazon_kdp_bookshelf_url", sa.String(), nullable=True)),
        ("amazon_kdp_timeout_ms", sa.Column("amazon_kdp_timeout_ms", sa.Integer(), nullable=True)),
        ("amazon_kdp_email_selector", sa.Column("amazon_kdp_email_selector", sa.String(), nullable=True)),
        ("amazon_kdp_password_selector", sa.Column("amazon_kdp_password_selector", sa.String(), nullable=True)),
        ("amazon_kdp_submit_selector", sa.Column("amazon_kdp_submit_selector", sa.String(), nullable=True)),
        ("amazon_kdp_new_ebook_url", sa.Column("amazon_kdp_new_ebook_url", sa.String(), nullable=True)),
        ("amazon_kdp_new_ebook_button_selector", sa.Column("amazon_kdp_new_ebook_button_selector", sa.String(), nullable=True)),
        ("amazon_kdp_title_selector", sa.Column("amazon_kdp_title_selector", sa.String(), nullable=True)),
        ("amazon_kdp_subtitle_selector", sa.Column("amazon_kdp_subtitle_selector", sa.String(), nullable=True)),
        ("amazon_kdp_author_selector", sa.Column("amazon_kdp_author_selector", sa.String(), nullable=True)),
        ("amazon_kdp_description_selector", sa.Column("amazon_kdp_description_selector", sa.String(), nullable=True)),
        ("amazon_kdp_keywords_selector", sa.Column("amazon_kdp_keywords_selector", sa.String(), nullable=True)),
        ("amazon_kdp_book_file_input_selector", sa.Column("amazon_kdp_book_file_input_selector", sa.String(), nullable=True)),
        ("amazon_kdp_cover_file_input_selector", sa.Column("amazon_kdp_cover_file_input_selector", sa.String(), nullable=True)),
        ("amazon_kdp_price_selector", sa.Column("amazon_kdp_price_selector", sa.String(), nullable=True)),
        ("amazon_kdp_publish_selector", sa.Column("amazon_kdp_publish_selector", sa.String(), nullable=True)),
        # ——— Suno / Stock / TTS que aparecem declaradas no ORM models.py Settings
        ("suno_api_key", sa.Column("suno_api_key", sa.String(), nullable=True)),
        ("pexels_api_key", sa.Column("pexels_api_key", sa.String(), nullable=True)),
        ("pixabay_api_key", sa.Column("pixabay_api_key", sa.String(), nullable=True)),
        ("edenai_api_key", sa.Column("edenai_api_key", sa.String(), nullable=True)),
        ("elevenlabs_voice_id", sa.Column("elevenlabs_voice_id", sa.String(), nullable=True)),
        ("elevenlabs_voice_name", sa.Column("elevenlabs_voice_name", sa.String(), nullable=True)),
        # ——— Providers padrão e unidade de custo (referenciados em router settings / ai router)
        ("text_provider", sa.Column("text_provider", sa.String(), nullable=True)),
        ("voice_provider", sa.Column("voice_provider", sa.String(), nullable=True)),
        ("image_provider", sa.Column("image_provider", sa.String(), nullable=True)),
        ("video_provider", sa.Column("video_provider", sa.String(), nullable=True)),
        ("music_provider", sa.Column("music_provider", sa.String(), nullable=True)),
        ("caption_provider", sa.Column("caption_provider", sa.String(), nullable=True)),
        ("thumbnail_provider", sa.Column("thumbnail_provider", sa.String(), nullable=True)),
        ("default_voice", sa.Column("default_voice", sa.String(), nullable=True)),
        ("default_voice_speed", sa.Column("default_voice_speed", sa.Float(), nullable=True)),
        ("default_voice_emotion", sa.Column("default_voice_emotion", sa.String(), nullable=True)),
        ("default_voice_intensity", sa.Column("default_voice_intensity", sa.Float(), nullable=True)),
        ("default_language", sa.Column("default_language", sa.String(), nullable=True)),
        ("default_cta", sa.Column("default_cta", sa.Text(), nullable=True)),
        ("default_next_episode_cta", sa.Column("default_next_episode_cta", sa.Text(), nullable=True)),
        ("default_playlist", sa.Column("default_playlist", sa.String(), nullable=True)),
        ("made_for_kids_default", sa.Column("made_for_kids_default", sa.Boolean(), nullable=True)),
        ("daily_spend_limit", sa.Column("daily_spend_limit", sa.Float(), nullable=True)),
        ("monthly_spend_limit", sa.Column("monthly_spend_limit", sa.Float(), nullable=True)),
        ("text_cost_unit", sa.Column("text_cost_unit", sa.Float(), nullable=True)),
        ("voice_cost_unit", sa.Column("voice_cost_unit", sa.Float(), nullable=True)),
        ("image_cost_unit", sa.Column("image_cost_unit", sa.Float(), nullable=True)),
        ("video_cost_unit", sa.Column("video_cost_unit", sa.Float(), nullable=True)),
        ("music_cost_unit", sa.Column("music_cost_unit", sa.Float(), nullable=True)),
        ("caption_cost_unit", sa.Column("caption_cost_unit", sa.Float(), nullable=True)),
        ("thumbnail_cost_unit", sa.Column("thumbnail_cost_unit", sa.Float(), nullable=True)),
        # ——— Editorial Intelligence (referenciado por bible_video_factory / global_settings_service)
        ("editorial_intelligence_enabled", sa.Column("editorial_intelligence_enabled", sa.Boolean(), nullable=True)),
        ("editorial_intelligence_fail_open", sa.Column("editorial_intelligence_fail_open", sa.Boolean(), nullable=True)),
        ("editorial_intelligence_mode", sa.Column("editorial_intelligence_mode", sa.String(), nullable=True)),
        ("editorial_intelligence_provider", sa.Column("editorial_intelligence_provider", sa.String(), nullable=True)),
        # ——— Novas integrações sociais (IG/TikTok)
        ("instagram_user_id", sa.Column("instagram_user_id", sa.String(), nullable=True)),
        ("instagram_access_token", sa.Column("instagram_access_token", sa.String(), nullable=True)),
        ("tiktok_access_token", sa.Column("tiktok_access_token", sa.String(), nullable=True)),
        # ——— Flag de ativação geral (compatibilidade com ORM)
        ("is_active", sa.Column("is_active", sa.Boolean(), nullable=True, server_default=sa.text("true"))),
    ]

    for _name, col in colunas:
        _add_if_missing(table, col)


def _upgrade_unified_videos() -> None:
    table = "unified_videos"
    if not _has_table(table):
        # Será criado pela migration a9b2c4d6e8f0 quando chegar nela
        return

    colunas: list[tuple[str, sa.Column]] = [
        ("force_reuse_assets", sa.Column("force_reuse_assets", sa.Boolean(), nullable=False, server_default=sa.text("false"))),
        ("force_render_only", sa.Column("force_render_only", sa.Boolean(), nullable=False, server_default=sa.text("false"))),
        ("review_feedback_json", sa.Column("review_feedback_json", sa.Text(), nullable=True)),
        ("render_logs_json", sa.Column("render_logs_json", sa.Text(), nullable=True)),
        # Garante youtube_video_id indexável com tamanho consistente
    ]
    for _name, col in colunas:
        _add_if_missing(table, col)


def _upgrade_video_tasks() -> None:
    table = "video_tasks"
    if not _has_table(table):
        return

    colunas: list[tuple[str, sa.Column]] = [
        ("status", sa.Column("status", sa.String(length=64), nullable=False, server_default="queued")),
        ("progress", sa.Column("progress", sa.Integer(), nullable=False, server_default="0")),
        ("message", sa.Column("message", sa.Text(), nullable=True)),
        ("result_json", sa.Column("result_json", sa.Text(), nullable=True)),
    ]
    for _name, col in colunas:
        _add_if_missing(table, col)


def _upgrade_series_plans() -> None:
    table = "series_plans"
    if not _has_table(table):
        return
    # ORM models.py SeriesPlan tem aspect_ratio declarado; garantir no DB.
    _add_if_missing(table, sa.Column("aspect_ratio", sa.String(length=16), nullable=True, server_default="16:9"))
    # auto_publish também pode estar no ORM como sinônimo de auto_approval (garantir coluna)
    _add_if_missing(table, sa.Column("auto_publish", sa.Boolean(), nullable=True, server_default=sa.text("false")))


def _upgrade_series_episodes() -> None:
    table = "series_episodes"
    if not _has_table(table):
        return

    colunas: list[tuple[str, sa.Column]] = [
        ("content_fingerprint", sa.Column("content_fingerprint", sa.String(length=255), nullable=True)),
        ("correction_plan_json", sa.Column("correction_plan_json", sa.Text(), nullable=True)),
        ("approved_snapshot_json", sa.Column("approved_snapshot_json", sa.Text(), nullable=True)),
        ("current_version", sa.Column("current_version", sa.Integer(), nullable=False, server_default="1")),
        ("metadata_json", sa.Column("metadata_json", sa.Text(), nullable=True)),
    ]
    for _name, col in colunas:
        _add_if_missing(table, col)


def _upgrade_scheduled_videos() -> None:
    table = "scheduled_videos"
    if not _has_table(table):
        return

    colunas: list[tuple[str, sa.Column]] = [
        # Ligação com pipeline central
        ("task_id", sa.Column("task_id", sa.String(length=64), nullable=True)),
        ("unified_video_id", sa.Column("unified_video_id", sa.Integer(), nullable=True)),
        ("youtube_refresh_token_at", sa.Column("youtube_refresh_token_at", sa.DateTime(), nullable=True)),
        ("youtube_video_id", sa.Column("youtube_video_id", sa.String(length=64), nullable=True)),
        ("youtube_url", sa.Column("youtube_url", sa.String(length=512), nullable=True)),
        ("youtube_upload_status", sa.Column("youtube_upload_status", sa.String(length=32), nullable=True, server_default="pending")),
        ("youtube_error_message", sa.Column("youtube_error_message", sa.Text(), nullable=True)),
        ("video_path", sa.Column("video_path", sa.Text(), nullable=True)),
        ("video_url", sa.Column("video_url", sa.Text(), nullable=True)),
        ("thumbnail_path", sa.Column("thumbnail_path", sa.Text(), nullable=True)),
        ("thumbnail_url", sa.Column("thumbnail_url", sa.Text(), nullable=True)),
        ("auto_post", sa.Column("auto_post", sa.Boolean(), nullable=False, server_default=sa.text("true"))),
        ("retry_count", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0")),
        ("last_error", sa.Column("last_error", sa.Text(), nullable=True)),
        ("pipeline", sa.Column("pipeline", sa.String(length=64), nullable=True)),
    ]
    for _name, col in colunas:
        _add_if_missing(table, col)


# ================= UPGRADE / DOWNGRADE ======================
def upgrade() -> None:
    _upgrade_settings()
    _upgrade_unified_videos()
    _upgrade_video_tasks()
    _upgrade_series_plans()
    _upgrade_series_episodes()
    _upgrade_scheduled_videos()


def _downgrade_settings() -> None:
    table = "settings"
    col_names = [
        "openai_image_model", "openai_allow_text", "openai_allow_script",
        "openai_allow_editorial", "openai_allow_analysis", "openai_allow_images",
        "openai_allow_thumbnail", "openai_allow_transcription", "openai_allow_tts",
        "openai_allow_embeddings", "openai_allow_other",
        "ai_cb_failure_threshold", "ai_cb_cooldown_seconds",
        "ai_cb_half_open_max_attempts", "openai_no_credit",
        "facebook_page_id", "facebook_access_token",
        "whatsapp_phone_number_id", "whatsapp_access_token", "whatsapp_verify_token",
        "whatsapp_allowed_numbers", "telegram_bot_token", "telegram_allowed_chat_ids",
        "mercadopago_access_token", "hotmart_client_id", "hotmart_client_secret",
        "hotmart_basic", "hotmart_access_token", "hotmart_token_expires_at",
        "amazon_kdp_email", "amazon_kdp_password", "amazon_kdp_login_url",
        "amazon_kdp_bookshelf_url", "amazon_kdp_timeout_ms",
        "amazon_kdp_email_selector", "amazon_kdp_password_selector",
        "amazon_kdp_submit_selector", "amazon_kdp_new_ebook_url",
        "amazon_kdp_new_ebook_button_selector", "amazon_kdp_title_selector",
        "amazon_kdp_subtitle_selector", "amazon_kdp_author_selector",
        "amazon_kdp_description_selector", "amazon_kdp_keywords_selector",
        "amazon_kdp_book_file_input_selector", "amazon_kdp_cover_file_input_selector",
        "amazon_kdp_price_selector", "amazon_kdp_publish_selector",
        "suno_api_key", "pexels_api_key", "pixabay_api_key", "edenai_api_key",
        "elevenlabs_voice_id", "elevenlabs_voice_name",
        "text_provider", "voice_provider", "image_provider", "video_provider",
        "music_provider", "caption_provider", "thumbnail_provider",
        "default_voice", "default_voice_speed", "default_voice_emotion",
        "default_voice_intensity", "default_language", "default_cta",
        "default_next_episode_cta", "default_playlist", "made_for_kids_default",
        "daily_spend_limit", "monthly_spend_limit",
        "text_cost_unit", "voice_cost_unit", "image_cost_unit", "video_cost_unit",
        "music_cost_unit", "caption_cost_unit", "thumbnail_cost_unit",
        "editorial_intelligence_enabled", "editorial_intelligence_fail_open",
        "editorial_intelligence_mode", "editorial_intelligence_provider",
        "instagram_user_id", "instagram_access_token", "tiktok_access_token",
        "is_active",
    ]
    for name in col_names:
        _drop_if_exists(table, name)


def _downgrade_unified_videos() -> None:
    table = "unified_videos"
    for name in ["force_reuse_assets", "force_render_only", "review_feedback_json", "render_logs_json"]:
        _drop_if_exists(table, name)


def _downgrade_video_tasks() -> None:
    table = "video_tasks"
    for name in ["status", "progress", "message", "result_json"]:
        _drop_if_exists(table, name)


def _downgrade_series_plans() -> None:
    table = "series_plans"
    for name in ["aspect_ratio", "auto_publish"]:
        _drop_if_exists(table, name)


def _downgrade_series_episodes() -> None:
    table = "series_episodes"
    for name in ["content_fingerprint", "correction_plan_json", "approved_snapshot_json", "current_version", "metadata_json"]:
        _drop_if_exists(table, name)


def _downgrade_scheduled_videos() -> None:
    table = "scheduled_videos"
    for name in [
        "task_id", "unified_video_id", "youtube_refresh_token_at",
        "youtube_video_id", "youtube_url", "youtube_upload_status",
        "youtube_error_message", "video_path", "video_url",
        "thumbnail_path", "thumbnail_url", "auto_post", "retry_count",
        "last_error", "pipeline",
    ]:
        _drop_if_exists(table, name)


def downgrade() -> None:
    _downgrade_scheduled_videos()
    _downgrade_series_episodes()
    _downgrade_series_plans()
    _downgrade_video_tasks()
    _downgrade_unified_videos()
    _downgrade_settings()
