import os
from typing import Any, Dict, Optional

from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models import Settings


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


def get_latest_settings(db=None) -> Optional[Settings]:
    if db is not None:
        try:
            return db.query(Settings).order_by(Settings.id.desc()).first()
        except Exception:
            return None
    local_db = SessionLocal()
    try:
        return local_db.query(Settings).order_by(Settings.id.desc()).first()
    except SQLAlchemyError:
        return None
    except Exception:
        return None
    finally:
        local_db.close()


def resolve_secret(
    attr_name: str,
    env_name: str,
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    settings_obj = settings if settings is not None else get_latest_settings()
    settings_raw = _normalize_text(getattr(settings_obj, attr_name, None) if settings_obj else None)
    settings_value = normalize_secret(settings_raw)
    env_raw = _normalize_text(os.getenv(env_name))
    env_value = normalize_secret(env_raw)
    if settings_value:
        return {
            "value": settings_value,
            "source": "settings",
            "configured": True,
            "invalid_placeholder": False,
            "settings_placeholder": False,
            "env_placeholder": is_placeholder_secret(env_raw),
        }
    if env_value:
        return {
            "value": env_value,
            "source": "env",
            "configured": True,
            "invalid_placeholder": False,
            "settings_placeholder": bool(settings_raw) and is_placeholder_secret(settings_raw),
            "env_placeholder": False,
        }
    return {
        "value": None,
        "source": None,
        "configured": False,
        "invalid_placeholder": (bool(settings_raw) and is_placeholder_secret(settings_raw)) or (bool(env_raw) and is_placeholder_secret(env_raw)),
        "settings_placeholder": bool(settings_raw) and is_placeholder_secret(settings_raw),
        "env_placeholder": bool(env_raw) and is_placeholder_secret(env_raw),
    }


def resolve_env_secret(env_name: str) -> Dict[str, Any]:
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


def resolve_optional_value(
    attr_name: str,
    env_name: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    settings_obj = settings if settings is not None else get_latest_settings()
    settings_raw = _normalize_text(getattr(settings_obj, attr_name, None) if settings_obj else None)
    if settings_raw:
        return {"value": settings_raw, "source": "settings"}
    env_raw = _normalize_text(os.getenv(env_name)) if env_name else ""
    return {"value": env_raw or None, "source": "env" if env_raw else None}


def resolve_global_provider_settings(settings: Optional[Settings] = None) -> Dict[str, Any]:
    settings_obj = settings if settings is not None else get_latest_settings()
    return {
        "settings": settings_obj,
        "openrouter_api_key": resolve_secret("openrouter_api_key", "OPENROUTER_API_KEY", settings_obj),
        "openai_api_key": resolve_secret("openai_api_key", "OPENAI_API_KEY", settings_obj),
        "elevenlabs_api_key": resolve_secret("elevenlabs_api_key", "ELEVENLABS_API_KEY", settings_obj),
        "edenai_api_key": resolve_secret("edenai_api_key", "EDENAI_API_KEY", settings_obj),
        "suno_api_key": resolve_secret("suno_api_key", "SUNO_API_KEY", settings_obj),
        "pexels_api_key": resolve_secret("pexels_api_key", "PEXELS_API_KEY", settings_obj),
        "pixabay_api_key": resolve_secret("pixabay_api_key", "PIXABAY_API_KEY", settings_obj),
        "huggingface_token": resolve_env_secret("HUGGINGFACE_TOKEN"),
        "openrouter_model": resolve_optional_value("openrouter_model", "OPENROUTER_MODEL", settings_obj),
        "ai_provider": resolve_optional_value("ai_provider", None, settings_obj),
        "elevenlabs_voice_id": resolve_optional_value("elevenlabs_voice_id", None, settings_obj),
        "elevenlabs_voice_name": resolve_optional_value("elevenlabs_voice_name", None, settings_obj),
        "instagram_access_token": resolve_secret("instagram_access_token", "INSTAGRAM_ACCESS_TOKEN", settings_obj),
        "instagram_user_id": resolve_optional_value("instagram_user_id", "INSTAGRAM_USER_ID", settings_obj),
        "tiktok_access_token": resolve_secret("tiktok_access_token", "TIKTOK_ACCESS_TOKEN", settings_obj),
        "youtube_refresh_token": resolve_secret("youtube_refresh_token", "YOUTUBE_REFRESH_TOKEN", settings_obj),
        "youtube_client_id": resolve_secret("youtube_client_id", "YOUTUBE_CLIENT_ID", settings_obj),
        "youtube_client_secret": resolve_secret("youtube_client_secret", "YOUTUBE_CLIENT_SECRET", settings_obj),
    }
