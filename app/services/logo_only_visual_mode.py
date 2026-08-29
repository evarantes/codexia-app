"""Global, fail-closed visual override for Codexia image-generating flows.

When a request/job explicitly enables ``logo_only_visuals``, image providers must
not be called. The official channel logo becomes the only visual asset. The
mode is opt-in per request/job; normal production is unchanged when disabled.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple


_HEADER_VALUES = {"1", "true", "yes", "on"}
_context: contextvars.ContextVar[Optional[Dict[str, str]]] = contextvars.ContextVar(
    "codexia_logo_only_visual_mode",
    default=None,
)


class LogoOnlyVisualModeError(RuntimeError):
    pass


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _HEADER_VALUES


def payload_requests_logo_only(payload: Any) -> bool:
    return isinstance(payload, dict) and is_truthy(payload.get("logo_only_visuals"))


def _candidate_to_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    except Exception:
        pass
    try:
        from app.config import absolute_path_for_static

        candidate = absolute_path_for_static(raw)
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    except Exception:
        pass
    return ""


def _public_url_for_path(path: str, preferred_url: Any = None) -> str:
    preferred = str(preferred_url or "").strip()
    if preferred.startswith("/static/"):
        return preferred
    try:
        from app.config import STATIC_DIR

        static_root = Path(str(STATIC_DIR)).resolve()
        candidate = Path(path).resolve()
        rel = candidate.relative_to(static_root)
        return "/static/" + str(rel).replace(os.sep, "/")
    except Exception:
        return path


def resolve_official_logo(db=None, user_id: Optional[int] = None) -> Tuple[str, str]:
    """Return ``(absolute_path, public_url_or_path)`` for the official logo."""
    settings = None
    if db is not None:
        try:
            from app.models import Settings

            query = db.query(Settings)
            if user_id is not None and hasattr(Settings, "user_id"):
                settings = query.filter(Settings.user_id == int(user_id)).order_by(Settings.id.desc()).first()
            if settings is None:
                settings = query.order_by(Settings.id.desc()).first()
        except Exception:
            settings = None

    if settings is None and db is not None:
        try:
            from app.services.global_settings_service import get_latest_settings

            settings = get_latest_settings(db)
        except Exception:
            settings = None

    raw_path = str(getattr(settings, "official_channel_logo_path", None) or "").strip() if settings else ""
    raw_url = str(getattr(settings, "official_channel_logo_url", None) or "").strip() if settings else ""
    env_path = str(os.getenv("OFFICIAL_CHANNEL_LOGO_PATH") or "").strip()
    env_url = str(os.getenv("OFFICIAL_CHANNEL_LOGO_URL") or "").strip()

    for value, preferred in ((raw_path, raw_url), (raw_url, raw_url), (env_path, env_url), (env_url, env_url)):
        path = _candidate_to_path(value)
        if path:
            return path, _public_url_for_path(path, preferred)

    raise LogoOnlyVisualModeError(
        "Modo 'usar apenas a logo' está marcado, mas a logo oficial do canal não foi encontrada. "
        "Configure/envie a logo em Configurações antes de continuar."
    )


def apply_logo_only_to_payload(
    payload: Dict[str, Any],
    *,
    db=None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Normalize an opted-in payload so downstream renderers cannot request AI images."""
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    if not payload_requests_logo_only(normalized):
        return normalized

    logo_path, logo_url = resolve_official_logo(db=db, user_id=user_id)
    normalized["logo_only_visuals"] = True
    normalized["logo_only_logo_path"] = logo_path
    normalized["logo_only_logo_url"] = logo_url
    normalized["selected_images"] = [logo_path]
    normalized["custom_image_paths"] = [logo_path]
    normalized["image_mode"] = "single"
    normalized["image_count"] = 1
    normalized["thumbnail_path"] = logo_path
    normalized["disable_ai_image_generation"] = True
    normalized["disable_ai_thumbnail_generation"] = True
    return normalized


@contextlib.contextmanager
def logo_only_visual_context(
    enabled: bool,
    *,
    db=None,
    user_id: Optional[int] = None,
    logo_path: Optional[str] = None,
    logo_url: Optional[str] = None,
) -> Iterator[Optional[Dict[str, str]]]:
    """Set request/job-local provider override; safe for concurrent requests."""
    if not enabled:
        token = _context.set(None)
        try:
            yield None
        finally:
            _context.reset(token)
        return

    resolved_path = _candidate_to_path(logo_path)
    resolved_url = str(logo_url or "").strip()
    if not resolved_path:
        resolved_path, resolved_url = resolve_official_logo(db=db, user_id=user_id)
    state = {
        "path": resolved_path,
        "url": resolved_url or _public_url_for_path(resolved_path),
    }
    token = _context.set(state)
    try:
        yield state
    finally:
        _context.reset(token)


def current_logo_only_visual() -> Optional[Dict[str, str]]:
    value = _context.get()
    return dict(value) if isinstance(value, dict) else None


def image_provider_override() -> Optional[str]:
    """Return the logo without making any provider call when context is enabled."""
    state = current_logo_only_visual()
    if not state:
        return None
    return str(state.get("url") or state.get("path") or "").strip() or None
