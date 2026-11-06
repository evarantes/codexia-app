"""API somente leitura do piloto Entre Nós Dois.

As rotas não fazem TTS, upload de imagem, geração de vídeo nem chamadas
a provedores. Exigem sessão administrativa autenticada.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.models import User
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
