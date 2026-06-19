from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Settings
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import requests
import base64
import os
import subprocess
import tempfile
import time

router = APIRouter(prefix="/settings", tags=["Settings"])


def _mask_configured(value: Optional[str]) -> bool:
    return bool(str(value or "").strip())


def _safe_float(value, digits: int = 2):
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _safe_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _provider_card(
    provider_id: str,
    label: str,
    configured: bool,
    billing_url: str,
    status: str = "missing",
    message: str = "",
    direct_api: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    card = {
        "id": provider_id,
        "label": label,
        "configured": bool(configured),
        "billing_url": billing_url,
        "status": status,
        "message": message or "",
        "direct_api": bool(direct_api),
    }
    if isinstance(extra, dict):
        card.update(extra)
    return card


def _fetch_openrouter_credits(api_key: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    card = _provider_card(
        "openrouter",
        "OpenRouter",
        True,
        "https://openrouter.ai/settings/credits",
        status="ok",
        direct_api=True,
    )
    try:
        key_resp = requests.get("https://openrouter.ai/api/v1/key", headers=headers, timeout=20)
        key_resp.raise_for_status()
        key_data = key_resp.json().get("data", {}) if isinstance(key_resp.json(), dict) else {}
        credit_resp = requests.get("https://openrouter.ai/api/v1/credits", headers=headers, timeout=20)
        credit_payload = {}
        if credit_resp.ok:
            raw = credit_resp.json()
            if isinstance(raw, dict):
                credit_payload = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}

        total_credits = _safe_float(credit_payload.get("total_credits"))
        total_usage = _safe_float(credit_payload.get("total_usage"))
        limit_remaining = _safe_float(key_data.get("limit_remaining"))
        limit_value = _safe_float(key_data.get("limit"))
        estimated_balance = None
        if total_credits is not None and total_usage is not None:
            estimated_balance = round(total_credits - total_usage, 2)
        card.update({
            "label_detail": key_data.get("label") or "Chave atual",
            "unit": "USD",
            "limit_remaining": limit_remaining,
            "limit": limit_value,
            "estimated_balance": estimated_balance,
            "total_credits": total_credits,
            "total_usage": total_usage,
            "usage_daily": _safe_float(key_data.get("usage_daily")),
            "usage_weekly": _safe_float(key_data.get("usage_weekly")),
            "usage_monthly": _safe_float(key_data.get("usage_monthly")),
            "is_free_tier": bool(key_data.get("is_free_tier")),
            "limit_reset": key_data.get("limit_reset"),
            "message": "Saldo consultado com sucesso.",
        })
        return card
    except Exception as e:
        card["status"] = "error"
        card["message"] = f"Falha ao consultar OpenRouter: {str(e)[:240]}"
        return card


def _fetch_elevenlabs_credits(api_key: str) -> Dict[str, Any]:
    headers = {"xi-api-key": api_key.strip()}
    card = _provider_card(
        "elevenlabs",
        "ElevenLabs",
        True,
        "https://elevenlabs.io/app/subscription",
        status="ok",
        direct_api=True,
    )
    try:
        resp = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json() if isinstance(resp.json(), dict) else {}
        used = _safe_int(data.get("character_count"))
        limit_value = _safe_int(data.get("character_limit"))
        remaining = None
        if used is not None and limit_value is not None:
            remaining = max(0, limit_value - used)
        card.update({
            "tier": data.get("tier"),
            "status_detail": data.get("status"),
            "used": used,
            "limit": limit_value,
            "remaining": remaining,
            "unit": "caracteres",
            "next_reset_unix": _safe_int(data.get("next_character_count_reset_unix")),
            "voice_slots_used": _safe_int(data.get("voice_slots_used")),
            "voice_limit": _safe_int(data.get("voice_limit")),
            "message": "Uso e limite consultados com sucesso.",
        })
        return card
    except Exception as e:
        card["status"] = "error"
        card["message"] = f"Falha ao consultar ElevenLabs: {str(e)[:240]}"
        return card


def _fetch_suno_credits(api_key: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    card = _provider_card(
        "suno",
        "Suno",
        True,
        "https://sunoapi.org/api-key",
        status="ok",
        direct_api=True,
    )
    try:
        resp = requests.get("https://api.sunoapi.org/api/v1/generate/credit", headers=headers, timeout=20)
        resp.raise_for_status()
        payload = resp.json() if isinstance(resp.json(), dict) else {}
        credits = _safe_int(payload.get("data"))
        card.update({
            "remaining": credits,
            "unit": "creditos",
            "message": "Creditos consultados com sucesso." if credits is not None else "Resposta recebida, mas sem saldo numerico.",
        })
        return card
    except Exception as e:
        card["status"] = "error"
        card["message"] = f"Falha ao consultar Suno: {str(e)[:240]}"
        return card

class SettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = None
    leonardo_api_key: Optional[str] = None
    leonardo_model_id: Optional[str] = None
    gemini_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    mistral_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    openrouter_model: Optional[str] = None
    ai_provider: Optional[str] = None
    facebook_page_id: Optional[str] = None
    facebook_access_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_access_token: Optional[str] = None
    whatsapp_verify_token: Optional[str] = None
    whatsapp_allowed_numbers: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_allowed_chat_ids: Optional[str] = None
    mercadopago_access_token: Optional[str] = None
    youtube_client_id: Optional[str] = None
    youtube_client_secret: Optional[str] = None
    youtube_refresh_token: Optional[str] = None
    youtube_auto_thanks_enabled: Optional[bool] = None
    youtube_auto_thanks_template: Optional[str] = None
    youtube_auto_thanks_max_per_run: Optional[int] = None
    youtube_auto_thanks_cooldown_hours: Optional[int] = None
    hotmart_client_id: Optional[str] = None
    hotmart_client_secret: Optional[str] = None
    hotmart_basic: Optional[str] = None
    amazon_kdp_email: Optional[str] = None
    amazon_kdp_password: Optional[str] = None
    amazon_kdp_login_url: Optional[str] = None
    amazon_kdp_bookshelf_url: Optional[str] = None
    amazon_kdp_timeout_ms: Optional[int] = None
    amazon_kdp_email_selector: Optional[str] = None
    amazon_kdp_password_selector: Optional[str] = None
    amazon_kdp_submit_selector: Optional[str] = None
    amazon_kdp_new_ebook_url: Optional[str] = None
    amazon_kdp_new_ebook_button_selector: Optional[str] = None
    amazon_kdp_title_selector: Optional[str] = None
    amazon_kdp_subtitle_selector: Optional[str] = None
    amazon_kdp_author_selector: Optional[str] = None
    amazon_kdp_description_selector: Optional[str] = None
    amazon_kdp_keywords_selector: Optional[str] = None
    amazon_kdp_book_file_input_selector: Optional[str] = None
    amazon_kdp_cover_file_input_selector: Optional[str] = None
    amazon_kdp_price_selector: Optional[str] = None
    amazon_kdp_publish_selector: Optional[str] = None
    suno_api_key: Optional[str] = None
    # Stock Media & TTS
    pexels_api_key: Optional[str] = None
    pixabay_api_key: Optional[str] = None
    edenai_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_voice_name: Optional[str] = None
    # Instagram
    instagram_user_id: Optional[str] = None
    instagram_access_token: Optional[str] = None
    # TikTok
    tiktok_access_token: Optional[str] = None

@router.get("/")
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(Settings).order_by(Settings.id.desc()).first()
    if not settings:
        # Criar configurações padrão se não existirem
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("/ai-credits")
def get_ai_credits(db: Session = Depends(get_db)):
    settings = db.query(Settings).order_by(Settings.id.desc()).first()
    providers: List[Dict[str, Any]] = []

    openrouter_key = (getattr(settings, "openrouter_api_key", None) or os.getenv("OPENROUTER_API_KEY") or "").strip()
    elevenlabs_key = (getattr(settings, "elevenlabs_api_key", None) or os.getenv("ELEVENLABS_API_KEY") or "").strip()
    suno_key = (getattr(settings, "suno_api_key", None) or os.getenv("SUNO_API_KEY") or "").strip()
    openai_key = (getattr(settings, "openai_api_key", None) or os.getenv("OPENAI_API_KEY") or "").strip()
    edenai_key = (getattr(settings, "edenai_api_key", None) or os.getenv("EDENAI_API_KEY") or "").strip()
    hf_token = (os.getenv("HUGGINGFACE_TOKEN") or "").strip()

    providers.append(
        _fetch_openrouter_credits(openrouter_key) if openrouter_key else _provider_card(
            "openrouter",
            "OpenRouter",
            False,
            "https://openrouter.ai/settings/credits",
            status="missing",
            message="Chave nao configurada.",
            direct_api=True,
        )
    )
    providers.append(
        _fetch_elevenlabs_credits(elevenlabs_key) if elevenlabs_key else _provider_card(
            "elevenlabs",
            "ElevenLabs",
            False,
            "https://elevenlabs.io/app/subscription",
            status="missing",
            message="Chave nao configurada.",
            direct_api=True,
        )
    )
    providers.append(
        _fetch_suno_credits(suno_key) if suno_key else _provider_card(
            "suno",
            "Suno",
            False,
            "https://sunoapi.org/api-key",
            status="missing",
            message="Chave nao configurada.",
            direct_api=True,
        )
    )
    providers.append(_provider_card(
        "openai",
        "OpenAI",
        _mask_configured(openai_key),
        "https://platform.openai.com/account/billing",
        status="portal_only" if _mask_configured(openai_key) else "missing",
        message="Abra o portal para recarga e auto recharge. O sistema pode adicionar link direto, mas nao consulta saldo simples por esta chave comum.",
        direct_api=False,
    ))
    providers.append(_provider_card(
        "edenai",
        "Eden AI",
        _mask_configured(edenai_key),
        "https://app.edenai.run/",
        status="portal_only" if _mask_configured(edenai_key) else "missing",
        message="Use o dashboard da Eden AI para acompanhar billing e consumo.",
        direct_api=False,
    ))
    providers.append(_provider_card(
        "huggingface",
        "Hugging Face",
        _mask_configured(hf_token),
        "https://huggingface.co/settings/billing",
        status="portal_only" if _mask_configured(hf_token) else "missing",
        message="Use a pagina de billing do Hugging Face para acompanhar os creditos e gastos do roteador.",
        direct_api=False,
    ))

    return {
        "generated_at": int(time.time()),
        "providers": providers,
    }

@router.post("/")
def update_settings(settings_update: SettingsUpdate, db: Session = Depends(get_db)):
    settings = db.query(Settings).order_by(Settings.id.desc()).first()
    if not settings:
        settings = Settings()
        db.add(settings)
    
    if settings_update.openai_api_key is not None:
        v = str(settings_update.openai_api_key).strip()
        settings.openai_api_key = v or None
    if settings_update.leonardo_api_key is not None:
        v = str(settings_update.leonardo_api_key).strip()
        settings.leonardo_api_key = v or None
    if settings_update.leonardo_model_id is not None:
        v = str(settings_update.leonardo_model_id).strip()
        settings.leonardo_model_id = v or None
    if settings_update.gemini_api_key is not None:
        settings.gemini_api_key = settings_update.gemini_api_key
    if settings_update.deepseek_api_key is not None:
        settings.deepseek_api_key = settings_update.deepseek_api_key
    if settings_update.groq_api_key is not None:
        settings.groq_api_key = settings_update.groq_api_key
    if settings_update.anthropic_api_key is not None:
        settings.anthropic_api_key = settings_update.anthropic_api_key
    if settings_update.mistral_api_key is not None:
        settings.mistral_api_key = settings_update.mistral_api_key
    if settings_update.openrouter_api_key is not None:
        settings.openrouter_api_key = settings_update.openrouter_api_key
    if settings_update.openrouter_model is not None:
        v = str(settings_update.openrouter_model).strip()
        settings.openrouter_model = v or None
    if settings_update.ai_provider is not None:
        settings.ai_provider = settings_update.ai_provider
    if settings_update.facebook_page_id is not None:
        settings.facebook_page_id = settings_update.facebook_page_id
    if settings_update.facebook_access_token is not None:
        settings.facebook_access_token = settings_update.facebook_access_token
    if settings_update.whatsapp_phone_number_id is not None:
        v = str(settings_update.whatsapp_phone_number_id).strip()
        settings.whatsapp_phone_number_id = v or None
    if settings_update.whatsapp_access_token is not None:
        v = str(settings_update.whatsapp_access_token).strip()
        settings.whatsapp_access_token = v or None
    if settings_update.whatsapp_verify_token is not None:
        v = str(settings_update.whatsapp_verify_token).strip()
        settings.whatsapp_verify_token = v or None
    if settings_update.whatsapp_allowed_numbers is not None:
        v = str(settings_update.whatsapp_allowed_numbers).strip()
        settings.whatsapp_allowed_numbers = v or None
    if settings_update.telegram_bot_token is not None:
        v = str(settings_update.telegram_bot_token).strip()
        settings.telegram_bot_token = v or None
    if settings_update.telegram_allowed_chat_ids is not None:
        v = str(settings_update.telegram_allowed_chat_ids).strip()
        settings.telegram_allowed_chat_ids = v or None
    if settings_update.mercadopago_access_token is not None:
        settings.mercadopago_access_token = settings_update.mercadopago_access_token
    # Evita apagar credenciais do YouTube por engano quando o frontend envia string vazia.
    if settings_update.youtube_client_id is not None:
        v = str(settings_update.youtube_client_id).strip()
        if v:
            settings.youtube_client_id = v
    if settings_update.youtube_client_secret is not None:
        v = str(settings_update.youtube_client_secret).strip()
        if v:
            settings.youtube_client_secret = v
    if settings_update.youtube_refresh_token is not None:
        v = str(settings_update.youtube_refresh_token).strip()
        if v:
            settings.youtube_refresh_token = v
    if settings_update.youtube_auto_thanks_enabled is not None:
        settings.youtube_auto_thanks_enabled = bool(settings_update.youtube_auto_thanks_enabled)
    if settings_update.youtube_auto_thanks_template is not None:
        v = str(settings_update.youtube_auto_thanks_template)
        settings.youtube_auto_thanks_template = v if v.strip() else None
    if settings_update.youtube_auto_thanks_max_per_run is not None:
        try:
            settings.youtube_auto_thanks_max_per_run = int(settings_update.youtube_auto_thanks_max_per_run)
        except Exception:
            pass
    if settings_update.youtube_auto_thanks_cooldown_hours is not None:
        try:
            settings.youtube_auto_thanks_cooldown_hours = int(settings_update.youtube_auto_thanks_cooldown_hours)
        except Exception:
            pass
    hotmart_changed = False
    if settings_update.hotmart_client_id is not None:
        v = str(settings_update.hotmart_client_id).strip()
        if v and v != (getattr(settings, "hotmart_client_id", None) or "").strip():
            settings.hotmart_client_id = v
            hotmart_changed = True
    if settings_update.hotmart_client_secret is not None:
        v = str(settings_update.hotmart_client_secret).strip()
        if v and v != (getattr(settings, "hotmart_client_secret", None) or "").strip():
            settings.hotmart_client_secret = v
            hotmart_changed = True
    if settings_update.hotmart_basic is not None:
        v = str(settings_update.hotmart_basic).strip()
        if v and v != (getattr(settings, "hotmart_basic", None) or "").strip():
            settings.hotmart_basic = v
            hotmart_changed = True
    if hotmart_changed:
        settings.hotmart_access_token = None
        settings.hotmart_token_expires_at = None
    if settings_update.amazon_kdp_email is not None:
        v = str(settings_update.amazon_kdp_email).strip()
        settings.amazon_kdp_email = v or None
    if settings_update.amazon_kdp_password is not None:
        v = str(settings_update.amazon_kdp_password)
        settings.amazon_kdp_password = v.strip() or None
    if settings_update.amazon_kdp_login_url is not None:
        v = str(settings_update.amazon_kdp_login_url).strip()
        settings.amazon_kdp_login_url = v or None
    if settings_update.amazon_kdp_bookshelf_url is not None:
        v = str(settings_update.amazon_kdp_bookshelf_url).strip()
        settings.amazon_kdp_bookshelf_url = v or None
    if settings_update.amazon_kdp_timeout_ms is not None:
        try:
            settings.amazon_kdp_timeout_ms = int(settings_update.amazon_kdp_timeout_ms)
        except Exception:
            pass
    for field in [
        "amazon_kdp_email_selector", "amazon_kdp_password_selector", "amazon_kdp_submit_selector",
        "amazon_kdp_new_ebook_url", "amazon_kdp_new_ebook_button_selector", "amazon_kdp_title_selector",
        "amazon_kdp_subtitle_selector", "amazon_kdp_author_selector", "amazon_kdp_description_selector",
        "amazon_kdp_keywords_selector", "amazon_kdp_book_file_input_selector",
        "amazon_kdp_cover_file_input_selector", "amazon_kdp_price_selector", "amazon_kdp_publish_selector"
    ]:
        val = getattr(settings_update, field, None)
        if val is not None:
            clean = str(val).strip()
            setattr(settings, field, clean or None)
    if settings_update.suno_api_key is not None:
        settings.suno_api_key = settings_update.suno_api_key

    if settings_update.pexels_api_key is not None:
        settings.pexels_api_key = settings_update.pexels_api_key
    if settings_update.pixabay_api_key is not None:
        settings.pixabay_api_key = settings_update.pixabay_api_key
    if settings_update.edenai_api_key is not None:
        v = str(settings_update.edenai_api_key).strip()
        settings.edenai_api_key = v or None
    if settings_update.elevenlabs_api_key is not None:
        settings.elevenlabs_api_key = settings_update.elevenlabs_api_key
    if settings_update.elevenlabs_voice_id is not None:
        v = str(settings_update.elevenlabs_voice_id).strip()
        settings.elevenlabs_voice_id = v or None
    if settings_update.elevenlabs_voice_name is not None:
        v = str(settings_update.elevenlabs_voice_name).strip()
        settings.elevenlabs_voice_name = v or None
    if settings_update.instagram_user_id is not None:
        v = str(settings_update.instagram_user_id).strip()
        settings.instagram_user_id = v or None
    if settings_update.instagram_access_token is not None:
        v = str(settings_update.instagram_access_token).strip()
        settings.instagram_access_token = v or None
    if settings_update.tiktok_access_token is not None:
        v = str(settings_update.tiktok_access_token).strip()
        settings.tiktok_access_token = v or None
    
    db.commit()
    db.refresh(settings)
    return settings

@router.post("/amazon/kdp/test")
def test_amazon_kdp_connection(db: Session = Depends(get_db)):
    settings = db.query(Settings).order_by(Settings.id.desc()).first()
    try:
        from app.services.distribution_automation import test_kdp_connection_via_browser
        result = test_kdp_connection_via_browser(settings)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/elevenlabs/voice")
def get_elevenlabs_voice(db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    return {
        "voice_id": (settings.elevenlabs_voice_id if settings else None),
        "voice_name": (settings.elevenlabs_voice_name if settings else None),
        "has_elevenlabs_key": bool(settings and (settings.elevenlabs_api_key or "").strip()),
    }

@router.post("/elevenlabs/voice")
async def create_elevenlabs_voice(
    name: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)

    api_key = (settings.elevenlabs_api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Configure a ElevenLabs API Key em Configurações antes de enviar amostra de voz.")

    voice_name = (name or "").strip()
    if not voice_name:
        raise HTTPException(status_code=400, detail="Informe um nome para a voz.")

    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Arquivo vazio.")

        original_name = (file.filename or "").strip() or "voice_sample"
        base, ext = os.path.splitext(original_name)
        if not ext:
            ct = (file.content_type or "").lower()
            if ct == "video/mp4":
                ext = ".mp4"
            elif ct in {"video/webm", "audio/webm"}:
                ext = ".webm"
            elif ct in {"video/quicktime"}:
                ext = ".mov"
            elif ct in {"audio/mpeg", "audio/mp3"}:
                ext = ".mp3"
            elif ct in {"audio/wav", "audio/x-wav"}:
                ext = ".wav"
            else:
                ext = ".bin"
        cleaned_filename = f"{base}{ext}"
        cleaned_bytes = content
        cleaned_content_type = file.content_type or "application/octet-stream"
        is_video = (cleaned_content_type or "").lower().startswith("video/")

        tmp_in_path = None
        tmp_out_path = None
        converted = False
        try:
            suffix = os.path.splitext(cleaned_filename)[1] or ".wav"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_in:
                tmp_in.write(content)
                tmp_in_path = tmp_in.name

            tmp_out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                tmp_in_path,
                "-t",
                "60",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-af",
                "highpass=f=80,lowpass=f=12000,afftdn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                tmp_out_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and os.path.exists(tmp_out_path) and os.path.getsize(tmp_out_path) > 0:
                with open(tmp_out_path, "rb") as f:
                    cleaned_bytes = f.read()
                cleaned_filename = "voice_sample_cleaned.mp3"
                cleaned_content_type = "audio/mpeg"
                converted = True
        except Exception:
            pass
        finally:
            for p in [tmp_in_path, tmp_out_path]:
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

        if is_video and not converted:
            raise HTTPException(
                status_code=400,
                detail="Não foi possível extrair o áudio do vídeo. Verifique se o ffmpeg está disponível no servidor ou envie um arquivo de áudio (mp3/wav).",
            )

        if len(cleaned_bytes) > 1_048_576:
            raise HTTPException(
                status_code=400,
                detail="A amostra ficou grande demais. Envie um trecho de até 60s (ideal) e com menos ruído.",
            )

        isolated_bytes = None
        isolated_content_type = None
        try:
            isolate_url = "https://api.elevenlabs.io/v1/audio-isolation/convert"
            isolate_headers = {"xi-api-key": api_key}
            isolate_files = {"audiofile": (cleaned_filename, cleaned_bytes, cleaned_content_type)}
            iso = requests.post(isolate_url, headers=isolate_headers, files=isolate_files, timeout=120)
            if iso.status_code < 400:
                ct = (iso.headers.get("content-type") or "").lower()
                if ct.startswith("application/json"):
                    payload = iso.json() or {}
                    b64 = payload.get("audio") or payload.get("audio_base64") or payload.get("data")
                    if isinstance(b64, str) and b64.strip():
                        isolated_bytes = base64.b64decode(b64)
                else:
                    isolated_bytes = iso.content
                    isolated_content_type = iso.headers.get("content-type") or "application/octet-stream"
        except Exception:
            isolated_bytes = None

        final_bytes = isolated_bytes if isolated_bytes else cleaned_bytes
        final_filename = "voice_sample_isolated.wav" if isolated_bytes else cleaned_filename
        final_content_type = isolated_content_type if isolated_bytes else cleaned_content_type
        if len(final_bytes) > 1_048_576:
            final_bytes = cleaned_bytes
            final_filename = cleaned_filename
            final_content_type = cleaned_content_type

        url = "https://api.elevenlabs.io/v1/voices/add"
        headers = {"xi-api-key": api_key}
        files = {"files": (final_filename, final_bytes, final_content_type)}
        data = {"name": voice_name}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=120)
        if r.status_code >= 400:
            detail = (r.text or "").strip()
            raise HTTPException(status_code=502, detail=f"ElevenLabs retornou erro ({r.status_code}): {detail[:500]}")

        payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        voice_id = (payload.get("voice_id") or payload.get("id") or "").strip()
        if not voice_id:
            raise HTTPException(status_code=502, detail="ElevenLabs não retornou voice_id.")

        settings.elevenlabs_voice_id = voice_id
        settings.elevenlabs_voice_name = voice_name
        db.commit()

        return {"voice_id": voice_id, "voice_name": voice_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao criar voz no ElevenLabs: {str(e)}")
