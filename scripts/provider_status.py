import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.models import Settings


def _has_value(value: Optional[str]) -> bool:
    return bool(str(value or "").strip())

def _env_flag(*names: str) -> bool:
    for name in names:
        raw = str(os.getenv(name) or "").strip().lower()
        if raw in {"1", "true", "yes", "sim", "on"}:
            return True
    return False


def _is_openrouter_model_explicit(value: Optional[str]) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    if raw in {"auto", "automatico", "automático", "best", "melhor", "openrouter/auto"}:
        return False
    return True


def _safe_model(value: Optional[str], fallback: str = "") -> str:
    raw = str(value or "").strip()
    return raw or fallback


def _get_cb_state(db) -> Dict[str, str]:
    try:
        rows = db.execute(
            """
            SELECT provider, state
            FROM ai_provider_circuit_breakers
            """
        )
        out = {}
        for r in rows:
            prov = str(r[0] or "").strip().lower()
            state = str(r[1] or "").strip().lower()
            if prov:
                out[prov] = state or "unknown"
        return out
    except Exception:
        return {}


def _provider_status(
    *,
    configured: bool,
    circuit_state: str,
    blocked_by_policy: bool = False,
    no_credit: bool = False,
) -> str:
    if blocked_by_policy:
        return "BLOCKED_BY_POLICY"
    if circuit_state == "open":
        return "CIRCUIT_OPEN"
    if not configured:
        return "NOT_CONFIGURED"
    if no_credit:
        return "CONFIGURED_NO_CREDIT"
    return "CONFIGURED_AVAILABLE"


def _collect_settings_snapshot(s: Settings) -> Dict[str, Any]:
    openrouter_model = _safe_model(getattr(s, "openrouter_model", None))
    openai_image_model = _safe_model(getattr(s, "openai_image_model", None))

    gemini_script_model = _safe_model(getattr(s, "gemini_script_model", None), "gemini-2.0-flash")
    gemini_text_model = _safe_model(getattr(s, "gemini_text_model", None), "gemini-2.0-flash")
    gemini_editorial_model = _safe_model(getattr(s, "gemini_editorial_model", None), "gemini-2.0-flash")
    gemini_analysis_model = _safe_model(getattr(s, "gemini_analysis_model", None), "gemini-2.0-flash")

    groq_transcription_model = _safe_model(getattr(s, "groq_transcription_model", None), "whisper-large-v3")

    db = SessionLocal()
    try:
        cb = _get_cb_state(db)
    finally:
        db.close()

    openai_key_present = _has_value(getattr(s, "openai_api_key", None)) or _has_value(os.getenv("OPENAI_API_KEY"))
    openai_no_credit = bool(getattr(s, "openai_no_credit", False)) or _env_flag("CODEXIA_OPENAI_NO_CREDIT", "OPENAI_NO_CREDIT")
    openai_blocked_all = not (
        bool(getattr(s, "openai_allow_images", True))
        or bool(getattr(s, "openai_allow_thumbnail", True))
        or bool(getattr(s, "openai_allow_text", False))
        or bool(getattr(s, "openai_allow_transcription", False))
    )

    gemini_key_present = _has_value(getattr(s, "gemini_api_key", None)) or _has_value(os.getenv("GEMINI_API_KEY"))
    openrouter_key_present = _has_value(getattr(s, "openrouter_api_key", None)) or _has_value(os.getenv("OPENROUTER_API_KEY"))
    groq_key_present = _has_value(getattr(s, "groq_api_key", None)) or _has_value(os.getenv("GROQ_API_KEY"))
    eleven_key_present = _has_value(getattr(s, "elevenlabs_api_key", None)) or _has_value(os.getenv("ELEVENLABS_API_KEY"))
    youtube_configured = (
        _has_value(getattr(s, "youtube_client_id", None))
        and _has_value(getattr(s, "youtube_client_secret", None))
        and _has_value(getattr(s, "youtube_refresh_token", None))
    )

    out: Dict[str, Any] = {
        "settings_id": int(getattr(s, "id", 0) or 0),
        "scope": "global" if getattr(s, "user_id", None) is None else f"user:{int(getattr(s, 'user_id', 0) or 0)}",
        "ai_router": {
            "circuit_breaker": {
                "failure_threshold": getattr(s, "ai_cb_failure_threshold", None),
                "cooldown_seconds": getattr(s, "ai_cb_cooldown_seconds", None),
                "half_open_max_attempts": getattr(s, "ai_cb_half_open_max_attempts", None),
            },
            "openai_allow": {
                "text": bool(getattr(s, "openai_allow_text", False)),
                "script": bool(getattr(s, "openai_allow_script", False)),
                "editorial": bool(getattr(s, "openai_allow_editorial", False)),
                "analysis": bool(getattr(s, "openai_allow_analysis", False)),
                "images": bool(getattr(s, "openai_allow_images", True)),
                "thumbnail": bool(getattr(s, "openai_allow_thumbnail", True)),
                "transcription": bool(getattr(s, "openai_allow_transcription", False)),
            },
        },
        "providers": {
            "gemini": {
                "configured": gemini_key_present,
                "models": {
                    "script": gemini_script_model,
                    "text": gemini_text_model,
                    "editorial": gemini_editorial_model,
                    "analysis": gemini_analysis_model,
                },
                "status": _provider_status(
                    configured=gemini_key_present,
                    circuit_state=str(cb.get("gemini", "closed")),
                ),
            },
            "openrouter": {
                "configured": openrouter_key_present,
                "model": openrouter_model,
                "model_is_explicit": _is_openrouter_model_explicit(openrouter_model),
                "status": _provider_status(
                    configured=openrouter_key_present,
                    circuit_state=str(cb.get("openrouter", "closed")),
                )
                if _is_openrouter_model_explicit(openrouter_model)
                else ("ERROR" if openrouter_key_present else "NOT_CONFIGURED"),
            },
            "groq": {
                "configured": groq_key_present,
                "transcription_model": groq_transcription_model,
                "status": _provider_status(
                    configured=groq_key_present,
                    circuit_state=str(cb.get("groq", "closed")),
                ),
            },
            "openai": {
                "configured": openai_key_present,
                "image_model": openai_image_model,
                "status": _provider_status(
                    configured=openai_key_present,
                    circuit_state=str(cb.get("openai", "closed")),
                    blocked_by_policy=openai_blocked_all,
                    no_credit=openai_no_credit,
                ),
            },
            "elevenlabs": {
                "configured": eleven_key_present,
                "voice_id_configured": _has_value(getattr(s, "elevenlabs_voice_id", None)),
                "voice_name": _safe_model(getattr(s, "elevenlabs_voice_name", None)),
                "status": _provider_status(
                    configured=eleven_key_present and _has_value(getattr(s, "elevenlabs_voice_id", None)),
                    circuit_state=str(cb.get("elevenlabs", "closed")),
                ),
            },
            "youtube": {
                "configured": youtube_configured,
                "status": "CONFIGURED_AVAILABLE" if youtube_configured else "NOT_CONFIGURED",
            },
        },
        "limits": {
            "daily_spend_limit": getattr(s, "daily_spend_limit", None),
            "monthly_spend_limit": getattr(s, "monthly_spend_limit", None),
            "per_video_spend_limit": getattr(s, "per_video_spend_limit", None),
        },
    }
    return out


def main() -> int:
    db = SessionLocal()
    try:
        s = db.query(Settings).order_by(Settings.id.desc()).first()
        if not s:
            print({"ok": False, "error": "NO_SETTINGS"})
            return 2
        snapshot = _collect_settings_snapshot(s)
        print({"ok": True, "snapshot": snapshot})

        infra_ok = True
        providers_ok = True
        missing: list = []

        if snapshot["providers"]["openrouter"]["configured"] and not snapshot["providers"]["openrouter"]["model_is_explicit"]:
            infra_ok = False
            providers_ok = False
            missing.append("openrouter_model_not_explicit")

        if snapshot["providers"]["gemini"]["status"] not in {"CONFIGURED_AVAILABLE"} and snapshot["providers"]["openrouter"]["status"] not in {"CONFIGURED_AVAILABLE"}:
            providers_ok = False
            missing.append("nenhum_provider_texto_disponivel(gemini_ou_openrouter)")

        print(
            {
                "infraestrutura_pronta_para_piloto": "SIM" if infra_ok else "NAO",
                "providers_prontos_para_piloto_real": "SIM" if providers_ok else "NAO",
                "pendencias": missing,
            }
        )
        return 0 if (infra_ok and providers_ok) else 3
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
