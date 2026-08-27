from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Settings, User
from app.routers.auth import get_current_admin_user
from app.services.narration_lab import NarrationLabError, narration_lab_service


router = APIRouter(prefix="/youtube/narration-lab", tags=["youtube", "narration-lab"])


class NarrationLabGenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    provider: Literal["edge_tts", "openai_tts", "elevenlabs"] = "edge_tts"
    voice: Optional[str] = "auto"
    voice_style: str = "human"
    voice_gender: Literal["female", "male"] = "female"
    confirm_paid_generation: bool = False


def _settings_for_user(db: Session, user_id: int):
    settings = (
        db.query(Settings)
        .filter(Settings.user_id == user_id)
        .order_by(Settings.id.desc())
        .first()
    )
    if settings is not None:
        return settings
    return db.query(Settings).order_by(Settings.id.desc()).first()


def _raise_lab_error(exc: NarrationLabError):
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.get("/options")
def narration_lab_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    settings = _settings_for_user(db, current_user.id)
    return narration_lab_service.provider_options(settings=settings)


@router.get("/samples")
def narration_lab_samples(
    limit: int = Query(12, ge=1, le=20),
    current_user: User = Depends(get_current_admin_user),
):
    return {"items": narration_lab_service.list_samples(user_id=current_user.id, limit=limit)}


@router.post("/generate")
def generate_narration_lab_sample(
    payload: NarrationLabGenerateRequest,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        return narration_lab_service.generate(payload.dict(), user_id=current_user.id)
    except NarrationLabError as exc:
        _raise_lab_error(exc)


@router.get("/audio/{sample_id}")
def get_narration_lab_audio(
    sample_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        path = narration_lab_service.audio_path(user_id=current_user.id, sample_id=sample_id)
    except NarrationLabError as exc:
        _raise_lab_error(exc)
    return FileResponse(
        str(path),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="amostra-narracao-{sample_id[:8]}.mp3"',
        },
    )
