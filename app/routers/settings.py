from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Settings
from pydantic import BaseModel
from typing import Optional
import requests

router = APIRouter(prefix="/settings", tags=["Settings"])

class SettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    mistral_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    ai_provider: Optional[str] = None
    facebook_page_id: Optional[str] = None
    facebook_access_token: Optional[str] = None
    mercadopago_access_token: Optional[str] = None
    youtube_client_id: Optional[str] = None
    youtube_client_secret: Optional[str] = None
    youtube_refresh_token: Optional[str] = None
    hotmart_client_id: Optional[str] = None
    hotmart_client_secret: Optional[str] = None
    suno_api_key: Optional[str] = None
    # Stock Media & TTS
    pexels_api_key: Optional[str] = None
    pixabay_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_voice_name: Optional[str] = None

@router.get("/")
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    if not settings:
        # Criar configurações padrão se não existirem
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings

@router.post("/")
def update_settings(settings_update: SettingsUpdate, db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
    
    if settings_update.openai_api_key is not None:
        settings.openai_api_key = settings_update.openai_api_key
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
    if settings_update.ai_provider is not None:
        settings.ai_provider = settings_update.ai_provider
    if settings_update.facebook_page_id is not None:
        settings.facebook_page_id = settings_update.facebook_page_id
    if settings_update.facebook_access_token is not None:
        settings.facebook_access_token = settings_update.facebook_access_token
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
    if settings_update.hotmart_client_id is not None:
        settings.hotmart_client_id = settings_update.hotmart_client_id
    if settings_update.hotmart_client_secret is not None:
        settings.hotmart_client_secret = settings_update.hotmart_client_secret
    if settings_update.suno_api_key is not None:
        settings.suno_api_key = settings_update.suno_api_key

    if settings_update.pexels_api_key is not None:
        settings.pexels_api_key = settings_update.pexels_api_key
    if settings_update.pixabay_api_key is not None:
        settings.pixabay_api_key = settings_update.pixabay_api_key
    if settings_update.elevenlabs_api_key is not None:
        settings.elevenlabs_api_key = settings_update.elevenlabs_api_key
    if settings_update.elevenlabs_voice_id is not None:
        v = str(settings_update.elevenlabs_voice_id).strip()
        settings.elevenlabs_voice_id = v or None
    if settings_update.elevenlabs_voice_name is not None:
        v = str(settings_update.elevenlabs_voice_name).strip()
        settings.elevenlabs_voice_name = v or None
    
    db.commit()
    db.refresh(settings)
    return settings

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
            raise HTTPException(status_code=400, detail="Arquivo de áudio vazio.")

        url = "https://api.elevenlabs.io/v1/voices/add"
        headers = {"xi-api-key": api_key}
        files = {
            "files": (file.filename or "voice_sample.wav", content, file.content_type or "application/octet-stream")
        }
        data = {"name": voice_name}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
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
