"""API administrativa do piloto Entre Nós Dois.

Oferece prévia editorial e armazenamento privado de imagens e áudios enviados
pelo administrador. Não faz geração paga, lip-sync ou publicação automática.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pathlib import Path
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Settings, UnifiedVideo
from app.services.provider_config import resolve_secret, normalize_secret
from app.services.entre_nos_dois_readiness import summarize_readiness
from app.services.entre_nos_dois_cost_worksheet import worksheet, CostWorksheetError
from app.services.entre_nos_dois_tts_single import submit_one_turn, PaidPilotTtsError
from app.services.entre_nos_dois_scene_readiness import lip_sync_readiness, LipSyncReadinessError
from app.services.entre_nos_dois_references import (
    StudioReferenceError, host_reference_status, store_host_reference, private_storage_health,
)
from app.services.entre_nos_dois_voice_check import run_voice_check
from app.services.entre_nos_dois_audio_review import (
    MAX_AUDIO_BYTES, StudioAudioError, list_segment_audio, store_segment_audio, preview_audio_timeline,
)
from app.routers.auth import get_current_admin_user
from app.services.entre_nos_dois import HOSTS, PILOT_TITLE, PROGRAM_DISCLOSURE, PROGRAM_TITLE
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest
from app.services.entre_nos_dois_drive import find_asset, stream_asset, EntreNosDoisDriveError

router = APIRouter(prefix="/studio/entre-nos-dois", tags=["studio-entre-nos-dois"])

@router.get("/painel", include_in_schema=False)
def studio_page():
    """HTML shell only. All pilot data endpoints remain JWT admin-protected."""
    path = Path(__file__).resolve().parents[1] / "static" / "pages" / "entre-nos-dois" / "index.html"
    return FileResponse(str(path), media_type="text/html", headers={"Cache-Control": "no-store"})



@router.get("/videos")
def project_videos(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Private project list; only videos explicitly created by this module appear."""
    rows = (
        db.query(UnifiedVideo)
        .filter(
            UnifiedVideo.user_id == current_user.id,
            UnifiedVideo.source_module == "entre_nos_dois",
        )
        .order_by(UnifiedVideo.created_at.desc(), UnifiedVideo.id.desc())
        .limit(100)
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "title": row.topic or "Episódio sem título",
                "content_type": row.content_type,
                "status": row.status,
                "progress": int(row.progress or 0),
                "current_step": row.current_step,
                "message": row.last_message,
                "error": row.last_error,
                "has_video": bool(row.video_url or row.video_path),
                "youtube_url": row.youtube_url,
                "published_at": row.published_at.isoformat() if row.published_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "auto_publish": False,
            }
            for row in rows
        ],
        "count": len(rows),
        "limit": 100,
        "auto_publish": False,
    }


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

@router.get("/voice-generation-gate")
def voice_generation_gate(current_user: User = Depends(get_current_admin_user)):
    """Non-secret administrative availability; no API calls or price guarantee."""
    enabled = os.getenv("CODEXIA_STUDIO_ENABLE_PAID_TTS") == "1"
    try:
        ceiling = float(os.getenv("CODEXIA_STUDIO_MAX_TTS_USD", "0"))
        enabled = enabled and 0 < ceiling <= 1000
    except ValueError:
        enabled = False
        ceiling = 0
    return {
        "enabled": enabled,
        "server_max_usd_per_turn": ceiling if enabled else None,
        "requires_account_verified_rate": True,
        "requires_explicit_per_turn_approval": True,
        "auto_publish": False,
    }


@router.get("/voice-cost-worksheet")
def voice_cost_worksheet(
    usd_per_1000_characters: str | None = None,
    current_user: User = Depends(get_current_admin_user),
):
    """Human cost worksheet only; not a price lookup or payment permission."""
    try:
        return worksheet(usd_per_1000_characters=usd_per_1000_characters)
    except CostWorksheetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


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
    storage_health = private_storage_health(user_id=current_user.id)
    storage_configured = storage_health["ready"]
    visual_assets = {}
    if storage_configured:
        try:
            visual_assets = host_reference_status(user_id=current_user.id)
        except (StudioReferenceError, OSError):
            storage_configured = False
    return summarize_readiness(
        elevenlabs_configured=bool(elevenlabs["configured"]),
        fal_configured=fal_configured,
        private_storage_configured=storage_configured,
        visual_assets=visual_assets,
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

@router.get("/references")
def references(current_user: User = Depends(get_current_admin_user)):
    """User-scoped private portrait presence only."""
    try:
        return {"hosts": host_reference_status(user_id=current_user.id)}
    except StudioReferenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.put("/references/{host}")
async def upload_reference(
    host: str,
    request: Request,
    current_user: User = Depends(get_current_admin_user),
):
    """Accept a raw image; validation, normalization and private storage only."""
    size = request.headers.get("content-length")
    if size is not None:
        try:
            if int(size) > 8 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Imagem excede 8 MB.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Tamanho inválido.") from None
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Imagem excede 8 MB.")
    try:
        return store_host_reference(user_id=current_user.id, host=host, body=bytes(data))
    except StudioReferenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/references/{host}/image")
def get_reference_image(host: str, current_user: User = Depends(get_current_admin_user)):
    """Private image delivery streamed from the user's isolated Drive folder."""
    if host not in {"Gabriel", "Helena"}:
        raise HTTPException(status_code=400, detail="Personagem inválido.")
    try:
        entry = find_asset(user_id=current_user.id, kind="image", filename=f"{host.lower()}.png")
        if not entry:
            raise HTTPException(status_code=404, detail="Imagem ainda não cadastrada.")
        response = stream_asset(entry, media_type="image/png")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except EntreNosDoisDriveError:
        raise HTTPException(status_code=503, detail="Imagem privada indisponível no Google Drive.") from None

@router.get("/audio")
def pilot_audio_status(current_user: User = Depends(get_current_admin_user)):
    """Metadata only. Never expose disk paths to the browser."""
    try:
        return list_segment_audio(user_id=current_user.id)
    except (StudioAudioError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Armazenamento privado de áudio indisponível.") from None



class PilotPaidVoiceApproval(BaseModel):
    quote_digest: str
    usd_per_1000_characters: str
    approved_max_usd: str
    confirmation: str
    rate_checked_in_provider_account: bool = False


@router.post("/audio/generate/{index}")
def generate_single_pilot_turn(
    index: int,
    approval: PilotPaidVoiceApproval,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Paid path is disabled unless the operator explicitly enables and caps it."""
    if os.getenv("CODEXIA_STUDIO_ENABLE_PAID_TTS") != "1":
        raise HTTPException(status_code=403, detail="Geração paga não habilitada.")
    settings = (
        db.query(Settings).filter(Settings.user_id == current_user.id)
        .order_by(Settings.id.desc()).first()
    )
    own_settings = settings if settings is not None else SimpleNamespace()
    credential = resolve_secret("elevenlabs_api_key", "ELEVENLABS_API_KEY", own_settings)
    try:
        return submit_one_turn(
            user_id=current_user.id, index=index,
            api_key=credential.get("value") or "",
            quote_digest=approval.quote_digest,
            usd_per_1000_characters=approval.usd_per_1000_characters,
            approved_max_usd=approval.approved_max_usd,
            confirmation=approval.confirmation,
            rate_checked_in_provider_account=approval.rate_checked_in_provider_account,
        )
    except (PaidPilotTtsError, CostWorksheetError, StudioAudioError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/scene/readiness")
def scene_readiness(
    usd_per_second: str | None = None,
    current_user: User = Depends(get_current_admin_user),
):
    """Private-media presence and scene costs only. Zero provider calls."""
    try:
        return lip_sync_readiness(
            user_id=current_user.id, usd_per_second=usd_per_second,
        )
    except LipSyncReadinessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (StudioReferenceError, StudioAudioError, OSError):
        raise HTTPException(status_code=503, detail="Armazenamento privado indisponível.") from None


@router.get("/audio/timeline")
def pilot_audio_timeline(current_user: User = Depends(get_current_admin_user)):
    """Measured private MP3 sequence; this is not a rendered video."""
    try:
        return preview_audio_timeline(user_id=current_user.id)
    except (StudioAudioError, OSError):
        raise HTTPException(status_code=503, detail="Linha do tempo de áudio indisponível.") from None


@router.put("/audio/{index}")
async def upload_pilot_audio(
    index: int,
    request: Request,
    current_user: User = Depends(get_current_admin_user),
):
    """Admin-controlled MP3 import; no provider calls and no paid work."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() not in {
        "audio/mpeg", "audio/mp3", "application/octet-stream",
    }:
        raise HTTPException(status_code=415, detail="Envie um arquivo MP3.")
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_AUDIO_BYTES:
                raise HTTPException(status_code=413, detail="MP3 acima de 10 MB.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Tamanho inválido.") from None
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="MP3 acima de 10 MB.")
    try:
        return store_segment_audio(user_id=current_user.id, index=index, body=bytes(data))
    except StudioAudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (StudioReferenceError, OSError):
        raise HTTPException(status_code=503, detail="Armazenamento privado indisponível.") from None


@router.get("/audio/{index}/file")
def play_pilot_audio(index: int, current_user: User = Depends(get_current_admin_user)):
    """Protected stream from Drive; no full-size MP3 is retained on the app volume."""
    if type(index) is not int or index < 1:
        raise HTTPException(status_code=400, detail="Trecho inválido.")
    try:
        entry = find_asset(user_id=current_user.id, kind="audio", filename=f"segment-{index:02d}.mp3")
        if not entry:
            raise HTTPException(status_code=404, detail="Trecho de áudio ainda não cadastrado.")
        response = stream_asset(entry, media_type="audio/mpeg")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except EntreNosDoisDriveError:
        raise HTTPException(status_code=503, detail="Áudio privado indisponível no Google Drive.") from None
