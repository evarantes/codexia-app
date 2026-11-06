"""API somente leitura do piloto Entre Nós Dois.

As rotas não fazem TTS, upload de imagem, geração de vídeo nem chamadas
a provedores. Exigem sessão administrativa autenticada.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Settings
from app.services.provider_config import resolve_secret, normalize_secret
from app.services.entre_nos_dois_readiness import summarize_readiness
from app.services.entre_nos_dois_voice_check import run_voice_check
from app.routers.auth import get_current_admin_user
from app.services.entre_nos_dois import HOSTS, PILOT_TITLE, PROGRAM_DISCLOSURE, PROGRAM_TITLE
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest

router = APIRouter(prefix="/studio/entre-nos-dois", tags=["studio-entre-nos-dois"])


@router.get("/overview")
def overview(current_user: User = Depends(get_current_admin_user)):
    manifest = prepare_speech_manifest()
    return {
        "program_title": PROGRAM_TITLE,
        "pilot_title": PILOT_TITLE,
        "disclosure": PROGRAM_DISCLOSURE,
        "hosts": [
            {
                "name": host.name,
                "voice_label": "Andrey" if host.name == "Gabriel" else "Luciana",
                "voice_configured": bool(host.provider_voice_id),
                "visual_reference_status": "pending_private_storage",
            }
            for host in HOSTS
        ],
        "dialogue": [
            {"speaker": item["speaker"], "text": item["text"], "target_seconds": item["target_seconds"]}
            for item in manifest["segments"]
        ],
        "estimated_characters": manifest["estimated_characters"],
        "target_duration_seconds": manifest["target_seconds"],
        "lip_sync_provider_candidate": "fal.ai Sync-3",
        "voice_access_verified_live": False,
        "audio_generated": False,
        "lip_sync_generated": False,
        "production_ready": False,
        "requires_human_cost_approval": True,
        "auto_publish": False,
    }

@router.get("/readiness")
def readiness(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Only tells the administrator whether configuration exists; no secrets or paid calls."""
    settings = (
        db.query(Settings)
        .filter(Settings.user_id == current_user.id)
        .order_by(Settings.id.desc())
        .first()
    )
    # Do not consult another user's global Settings when no user-specific row exists.
    own_settings = settings if settings is not None else SimpleNamespace()
    elevenlabs = resolve_secret("elevenlabs_api_key", "ELEVENLABS_API_KEY", own_settings)
    fal_configured = bool(normalize_secret(os.getenv("FAL_KEY")))
    return summarize_readiness(
        elevenlabs_configured=bool(elevenlabs["configured"]),
        fal_configured=fal_configured,
    )

@router.post("/verify-voices")
def verify_voices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admin opt-in: consults only voice metadata (GET) without running paid TTS.

    POST requires an intentional UI action; the provider API request uses GET.
    Only user-scoped credentials are eligible (no other user's Settings).
    """
    settings = (
        db.query(Settings)
        .filter(Settings.user_id == current_user.id)
        .order_by(Settings.id.desc())
        .first()
    )
    own_settings = settings if settings is not None else SimpleNamespace()
    resolved = resolve_secret("elevenlabs_api_key", "ELEVENLABS_API_KEY", own_settings)
    return run_voice_check(provider_settings={"elevenlabs_api_key": resolved})
