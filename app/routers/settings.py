from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Settings
from pydantic import BaseModel
from typing import Optional

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
    if settings_update.youtube_client_id is not None:
        settings.youtube_client_id = settings_update.youtube_client_id
    if settings_update.youtube_client_secret is not None:
        settings.youtube_client_secret = settings_update.youtube_client_secret
    if settings_update.youtube_refresh_token is not None:
        settings.youtube_refresh_token = settings_update.youtube_refresh_token
    
    db.commit()
    db.refresh(settings)
    return settings
