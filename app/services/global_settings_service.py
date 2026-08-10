import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Settings
from app.modules.bible_video_factory.editorial_intelligence import (
    DEFAULT_EDITORIAL_INTELLIGENCE_SETTINGS,
)

if TYPE_CHECKING:
    from app.modules.bible_video_factory.models import BibleVideoConfig


PLACEHOLDER_MARKERS = (
    "sua_chave",
    "your_api_key",
    "your-key",
    "placeholder",
    "changeme",
    "change_me",
    "replace_me",
    "insira_sua_chave",
    "coloque_sua_chave",
)

PLACEHOLDER_VALUES = {
    "sk-...",
    "sk-or-...",
    "api_key",
    "token",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def is_placeholder_secret(value: Any) -> bool:
    raw = _normalize_text(value)
    if not raw:
        return False
    lowered = raw.lower()
    compact = lowered.replace(" ", "").replace("-", "_")
    if lowered in PLACEHOLDER_VALUES or compact in PLACEHOLDER_VALUES:
        return True
    return any(marker in compact for marker in PLACEHOLDER_MARKERS)


def normalize_secret(value: Any) -> Optional[str]:
    raw = _normalize_text(value)
    if not raw or is_placeholder_secret(raw):
        return None
    return raw


def normalize_value(value: Any) -> Optional[str]:
    raw = _normalize_text(value)
    return raw or None


OFFICIAL_FACTORY_SETTINGS_DEFAULTS: Dict[str, Any] = {
    "text_provider": "openai",
    "voice_provider": "elevenlabs",
    "image_provider": "openai",
    "video_provider": "luma",
    "music_provider": "musicgen",
    "caption_provider": "native",
    "thumbnail_provider": "openai",
    "default_voice": None,
    "default_voice_speed": 1.0,
    "default_voice_emotion": None,
    "default_voice_intensity": 0.7,
    "default_language": "pt-BR",
    "default_cta": "Inscreva-se para acompanhar os proximos episodios biblicos.",
    "default_next_episode_cta": "No proximo episodio, a historia continua com mais tensao e revelacao.",
    "default_playlist": None,
    "made_for_kids_default": False,
    "daily_spend_limit": 0.0,
    "monthly_spend_limit": 0.0,
    "text_cost_unit": 0.0,
    "voice_cost_unit": 0.0,
    "image_cost_unit": 0.0,
    "video_cost_unit": 0.0,
    "music_cost_unit": 0.0,
    "caption_cost_unit": 0.0,
    "thumbnail_cost_unit": 0.0,
    **DEFAULT_EDITORIAL_INTELLIGENCE_SETTINGS,
}

OFFICIAL_FACTORY_TEXT_FIELDS = {
    "text_provider",
    "voice_provider",
    "image_provider",
    "video_provider",
    "music_provider",
    "caption_provider",
    "thumbnail_provider",
    "default_voice",
    "default_voice_emotion",
    "default_language",
    "default_cta",
    "default_next_episode_cta",
    "default_playlist",
    "editorial_intelligence_mode",
    "editorial_intelligence_provider",
    "primary_provider",
    "fallback_provider",
    "editorial_provider",
    "editorial_fallback_provider",
    "provider_priority",
    "approved_models",
}

OFFICIAL_FACTORY_FLOAT_FIELDS = {
    "default_voice_speed",
    "default_voice_intensity",
    "daily_spend_limit",
    "monthly_spend_limit",
    "text_cost_unit",
    "voice_cost_unit",
    "image_cost_unit",
    "video_cost_unit",
    "music_cost_unit",
    "caption_cost_unit",
    "thumbnail_cost_unit",
}

OFFICIAL_FACTORY_BOOL_FIELDS = {
    "made_for_kids_default",
    "editorial_intelligence_enabled",
    "editorial_intelligence_fail_open",
}

OFFICIAL_FACTORY_SETTINGS_FIELDS = tuple(OFFICIAL_FACTORY_SETTINGS_DEFAULTS.keys())


def get_latest_settings(db: Optional[Session] = None) -> Optional[Settings]:
    """Lê settings SEM deixar transação abortada na Session.
    - Se db vier de fora (request dependency): NÃO fecha; mas roda rollback em erro.
    - Se abrirmos SessionLocal aqui: fecha no finally.
    Evita InFailedSqlTransaction no próximo SELECT (coluna ausente, etc.)."""
    own_session = db is None
    local_db: Session = db if db is not None else SessionLocal()
    try:
        return local_db.query(Settings).order_by(Settings.id.desc()).first()
    except SQLAlchemyError:
        try:
            if local_db.is_active:
                local_db.rollback()
        except Exception:
            pass
        return None
    except Exception:
        try:
            if local_db.is_active:
                local_db.rollback()
        except Exception:
            pass
        return None
    finally:
        if own_session:
            try:
                local_db.close()
            except Exception:
                pass


def get_or_create_latest_settings(db: Session) -> Settings:
    """Get-or-create transacional para Settings:
    - rollback em erro (não deixa Session abortada);
    - NÃO sobrescreve valores nulos em campos recém-adicionados;
    - não loga secrets."""
    try:
        try:
            settings = get_latest_settings(db)
            if settings is not None:
                return settings
            settings = Settings()
            db.add(settings)
            db.commit()
            db.refresh(settings)
            return settings
        except SQLAlchemyError:
            try:
                db.rollback()
            except Exception:
                pass
            raise
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise


def get_latest_legacy_bible_video_config(
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> Optional["BibleVideoConfig"]:
    from app.modules.bible_video_factory.models import BibleVideoConfig

    own_session = db is None
    local_db: Session = db if db is not None else SessionLocal()
    try:
        query = local_db.query(BibleVideoConfig)
        if user_id is not None:
            query = query.filter(BibleVideoConfig.user_id == user_id)
        return query.order_by(BibleVideoConfig.id.desc()).first()
    except SQLAlchemyError:
        try:
            if local_db.is_active:
                local_db.rollback()
        except Exception:
            pass
        return None
    except Exception:
        try:
            if local_db.is_active:
                local_db.rollback()
        except Exception:
            pass
        return None
    finally:
        if own_session:
            try:
                local_db.close()
            except Exception:
                pass


def _clean_factory_text(value: Any) -> Optional[str]:
    raw = _normalize_text(value)
    return raw or None


def _clean_factory_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clean_factory_bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = _normalize_text(value).lower()
    if text in {"1", "true", "yes", "sim", "on"}:
        return True
    if text in {"0", "false", "no", "nao", "off"}:
        return False
    return bool(default)


def serialize_official_factory_settings(settings_obj: Optional[Settings]) -> Dict[str, Any]:
    payload = dict(OFFICIAL_FACTORY_SETTINGS_DEFAULTS)
    if not settings_obj:
        return payload
    for key, default in OFFICIAL_FACTORY_SETTINGS_DEFAULTS.items():
        raw_value = getattr(settings_obj, key, None)
        if key in OFFICIAL_FACTORY_BOOL_FIELDS:
            payload[key] = _clean_factory_bool(raw_value, bool(default))
        elif key in OFFICIAL_FACTORY_FLOAT_FIELDS:
            payload[key] = _clean_factory_float(raw_value, float(default))
        else:
            payload[key] = _clean_factory_text(raw_value) if raw_value is not None else default
            if payload[key] is None and default is not None:
                payload[key] = default
    payload.update(
        {
            "settings_id": getattr(settings_obj, "id", None),
            "user_id": getattr(settings_obj, "user_id", None),
        }
    )
    return payload


def apply_official_factory_settings(settings_obj: Settings, payload: Optional[Dict[str, Any]]) -> None:
    raw_payload = dict(payload or {})
    for key, default in OFFICIAL_FACTORY_SETTINGS_DEFAULTS.items():
        if key not in raw_payload:
            continue
        if key in OFFICIAL_FACTORY_BOOL_FIELDS:
            setattr(settings_obj, key, _clean_factory_bool(raw_payload.get(key), bool(default)))
        elif key in OFFICIAL_FACTORY_FLOAT_FIELDS:
            setattr(settings_obj, key, _clean_factory_float(raw_payload.get(key), float(default)))
        else:
            setattr(settings_obj, key, _clean_factory_text(raw_payload.get(key)))


def backfill_settings_from_legacy(
    db: Session,
    settings: Optional[Settings] = None,
    user_id: Optional[int] = None,
    legacy_bible_video_config: Optional["BibleVideoConfig"] = None,
) -> Settings:
    settings_obj = settings or get_or_create_latest_settings(db)
    legacy = legacy_bible_video_config or get_latest_legacy_bible_video_config(db, user_id=user_id)
    if legacy is None:
        return settings_obj

    changed = False

    for attr_name, legacy_value in {
        "openrouter_api_key": getattr(legacy, "text_api_key", None) if _normalize_text(getattr(legacy, "text_provider", None)).lower() == "openrouter" else None,
        "openai_api_key": getattr(legacy, "text_api_key", None) if _normalize_text(getattr(legacy, "text_provider", None)).lower() == "openai" else None,
        "elevenlabs_api_key": getattr(legacy, "voice_api_key", None) if _normalize_text(getattr(legacy, "voice_provider", None)).lower() == "elevenlabs" else None,
        "edenai_api_key": getattr(legacy, "voice_api_key", None) if _normalize_text(getattr(legacy, "voice_provider", None)).lower() == "edenai" else None,
    }.items():
        if getattr(settings_obj, attr_name, None):
            continue
        normalized = normalize_secret(legacy_value)
        if normalized:
            setattr(settings_obj, attr_name, normalized)
            changed = True

    for key, default in OFFICIAL_FACTORY_SETTINGS_DEFAULTS.items():
        current_value = getattr(settings_obj, key, None)
        legacy_value = getattr(legacy, key, None) if hasattr(legacy, key) else None
        if key in OFFICIAL_FACTORY_BOOL_FIELDS:
            has_current = current_value is not None
        elif key in OFFICIAL_FACTORY_FLOAT_FIELDS:
            has_current = current_value is not None
        else:
            has_current = bool(_normalize_text(current_value))
        if has_current:
            continue
        if legacy_value is None:
            continue
        if key in OFFICIAL_FACTORY_BOOL_FIELDS:
            setattr(settings_obj, key, _clean_factory_bool(legacy_value, bool(default)))
            changed = True
        elif key in OFFICIAL_FACTORY_FLOAT_FIELDS:
            setattr(settings_obj, key, _clean_factory_float(legacy_value, float(default)))
            changed = True
        else:
            normalized = _clean_factory_text(legacy_value)
            if normalized is not None:
                setattr(settings_obj, key, normalized)
                changed = True

    if changed:
        db.add(settings_obj)
        db.commit()
        db.refresh(settings_obj)
    return settings_obj


class GlobalSettingsService:
    def __init__(
        self,
        db: Optional[Session] = None,
        user_id: Optional[int] = None,
        settings: Optional[Settings] = None,
        legacy_bible_video_config: Optional["BibleVideoConfig"] = None,
    ) -> None:
        self.db = db
        self.user_id = user_id
        self._settings = settings
        self._legacy_bible_video_config = legacy_bible_video_config

    def get_settings(self) -> Optional[Settings]:
        if self._settings is None:
            self._settings = get_latest_settings(self.db)
        return self._settings

    def _result_from_sources(
        self,
        settings_raw: Any,
        legacy_raw: Any,
        env_raw: Any,
        *,
        allow_placeholders: bool = False,
    ) -> Dict[str, Any]:
        settings_text = _normalize_text(settings_raw)
        legacy_text = _normalize_text(legacy_raw)
        env_text = _normalize_text(env_raw)

        settings_value = settings_text if allow_placeholders else normalize_secret(settings_text)
        legacy_value = legacy_text if allow_placeholders else normalize_secret(legacy_text)
        env_value = env_text if allow_placeholders else normalize_secret(env_text)

        if settings_value:
            return {
                "value": settings_value,
                "source": "settings",
                "configured": True,
                "invalid_placeholder": False,
                "settings_placeholder": False,
                "env_placeholder": bool(env_text) and is_placeholder_secret(env_text),
            }
        if legacy_value:
            return {
                "value": legacy_value,
                "source": "legacy_bible_video_config",
                "configured": True,
                "invalid_placeholder": False,
                "settings_placeholder": bool(settings_text) and is_placeholder_secret(settings_text),
                "env_placeholder": bool(env_text) and is_placeholder_secret(env_text),
            }
        if env_value:
            return {
                "value": env_value,
                "source": "env",
                "configured": True,
                "invalid_placeholder": False,
                "settings_placeholder": bool(settings_text) and is_placeholder_secret(settings_text),
                "env_placeholder": False,
            }
        return {
            "value": None,
            "source": None,
            "configured": False,
            "invalid_placeholder": (
                (bool(settings_text) and is_placeholder_secret(settings_text))
                or (bool(legacy_text) and is_placeholder_secret(legacy_text))
                or (bool(env_text) and is_placeholder_secret(env_text))
            ),
            "settings_placeholder": bool(settings_text) and is_placeholder_secret(settings_text),
            "env_placeholder": bool(env_text) and is_placeholder_secret(env_text),
        }

    def resolve_secret(self, attr_name: str, env_name: str) -> Dict[str, Any]:
        settings_obj = self.get_settings()
        return self._result_from_sources(
            getattr(settings_obj, attr_name, None) if settings_obj else None,
            None,
            os.getenv(env_name),
        )

    def resolve_env_secret(self, env_name: str) -> Dict[str, Any]:
        env_raw = _normalize_text(os.getenv(env_name))
        env_value = normalize_secret(env_raw)
        if env_value:
            return {
                "value": env_value,
                "source": "env",
                "configured": True,
                "invalid_placeholder": False,
                "settings_placeholder": False,
                "env_placeholder": False,
            }
        return {
            "value": None,
            "source": None,
            "configured": False,
            "invalid_placeholder": bool(env_raw) and is_placeholder_secret(env_raw),
            "settings_placeholder": False,
            "env_placeholder": bool(env_raw) and is_placeholder_secret(env_raw),
        }

    def resolve_optional_value(self, attr_name: str, env_name: Optional[str] = None) -> Dict[str, Any]:
        settings_obj = self.get_settings()
        settings_raw = _normalize_text(getattr(settings_obj, attr_name, None) if settings_obj else None)
        if settings_raw:
            return {"value": settings_raw, "source": "settings"}

        env_raw = _normalize_text(os.getenv(env_name)) if env_name else ""
        return {"value": env_raw or None, "source": "env" if env_raw else None}

    def get_ai_provider_settings(self) -> Dict[str, Any]:
        settings_obj = self.get_settings()
        return {
            "settings": settings_obj,
            "openrouter_api_key": self.resolve_secret("openrouter_api_key", "OPENROUTER_API_KEY"),
            "openai_api_key": self.resolve_secret("openai_api_key", "OPENAI_API_KEY"),
            "elevenlabs_api_key": self.resolve_secret("elevenlabs_api_key", "ELEVENLABS_API_KEY"),
            "edenai_api_key": self.resolve_secret("edenai_api_key", "EDENAI_API_KEY"),
            "suno_api_key": self.resolve_secret("suno_api_key", "SUNO_API_KEY"),
            "pexels_api_key": self.resolve_secret("pexels_api_key", "PEXELS_API_KEY"),
            "pixabay_api_key": self.resolve_secret("pixabay_api_key", "PIXABAY_API_KEY"),
            "huggingface_token": self.resolve_env_secret("HUGGINGFACE_TOKEN"),
            "openrouter_model": self.resolve_optional_value("openrouter_model", "OPENROUTER_MODEL"),
            "ai_provider": self.resolve_optional_value("ai_provider", None),
            "elevenlabs_voice_id": self.resolve_optional_value("elevenlabs_voice_id", None),
            "elevenlabs_voice_name": self.resolve_optional_value("elevenlabs_voice_name", None),
            "instagram_access_token": self.resolve_secret("instagram_access_token", "INSTAGRAM_ACCESS_TOKEN"),
            "instagram_user_id": self.resolve_optional_value("instagram_user_id", "INSTAGRAM_USER_ID"),
            "tiktok_access_token": self.resolve_secret("tiktok_access_token", "TIKTOK_ACCESS_TOKEN"),
            "youtube_refresh_token": self.resolve_secret("youtube_refresh_token", "YOUTUBE_REFRESH_TOKEN"),
            "youtube_client_id": self.resolve_secret("youtube_client_id", "YOUTUBE_CLIENT_ID"),
            "youtube_client_secret": self.resolve_secret("youtube_client_secret", "YOUTUBE_CLIENT_SECRET"),
        }

    def get_bible_video_factory_settings(self) -> Dict[str, Any]:
        return serialize_official_factory_settings(self.get_settings())


def build_global_settings_service(
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
    settings: Optional[Settings] = None,
    legacy_bible_video_config: Optional["BibleVideoConfig"] = None,
) -> GlobalSettingsService:
    return GlobalSettingsService(
        db=db,
        user_id=user_id,
        settings=settings,
        legacy_bible_video_config=legacy_bible_video_config,
    )
