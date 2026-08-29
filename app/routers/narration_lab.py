from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Settings, User
from app.routers.auth import get_current_admin_user
from app.services.narration_lab import NarrationLabError, narration_lab_service
from app.services.youtube_narration_gate import (
    YouTubeNarrationGateError,
    youtube_narration_gate_service,
)


router = APIRouter(prefix="/youtube/narration-lab", tags=["youtube", "narration-lab"])


class NarrationLabGenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    provider: Literal["edge_tts", "openai_tts", "elevenlabs"] = "edge_tts"
    voice: Optional[str] = "auto"
    voice_style: str = "human"
    voice_gender: Literal["female", "male"] = "female"
    confirm_paid_generation: bool = False


class NarrationGateGenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=30000)
    voice: Optional[str] = "auto"
    voice_gender: Literal["female", "male"] = "female"


class NarrationGateApproveRequest(BaseModel):
    preview_id: str = Field(..., min_length=32, max_length=32)
    expected_text: str = Field(..., min_length=1, max_length=30000)


class NarrationLogoTestRequest(BaseModel):
    preview_id: str = Field(..., min_length=32, max_length=32)


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


def _official_logo_path(settings: Optional[Settings]) -> str:
    if settings is None:
        return ""
    raw_path = str(getattr(settings, "official_channel_logo_path", None) or "").strip()
    if raw_path:
        candidate = Path(raw_path).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    raw_url = str(getattr(settings, "official_channel_logo_url", None) or "").strip()
    for value in (raw_path, raw_url):
        if not value:
            continue
        try:
            from app.config import absolute_path_for_static
            resolved = absolute_path_for_static(value)
            if resolved and Path(resolved).is_file():
                return str(Path(resolved).resolve())
        except Exception:
            continue
    return ""


def _raise_lab_error(exc: NarrationLabError):
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


def _raise_gate_error(exc: YouTubeNarrationGateError):
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


@router.post("/production-preview")
def generate_production_narration_preview(
    payload: NarrationGateGenerateRequest,
    current_user: User = Depends(get_current_admin_user),
):
    """Generate the complete narration only; never enqueue/render/generate images."""
    try:
        return youtube_narration_gate_service.generate(
            text=payload.text,
            user_id=current_user.id,
            voice=payload.voice,
            voice_gender=payload.voice_gender,
        )
    except YouTubeNarrationGateError as exc:
        _raise_gate_error(exc)


@router.post("/production-preview/approve")
def approve_production_narration_preview(
    payload: NarrationGateApproveRequest,
    current_user: User = Depends(get_current_admin_user),
):
    """Approve the exact text/audio pair and expose a canonical reuse_audio_from payload."""
    try:
        return youtube_narration_gate_service.approve(
            preview_id=payload.preview_id,
            expected_text=payload.expected_text,
            user_id=current_user.id,
        )
    except YouTubeNarrationGateError as exc:
        _raise_gate_error(exc)


@router.get("/production-preview/audio/{preview_id}")
def get_production_narration_preview_audio(
    preview_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        path = youtube_narration_gate_service.audio_path(preview_id=preview_id, user_id=current_user.id)
    except YouTubeNarrationGateError as exc:
        _raise_gate_error(exc)
    return FileResponse(
        str(path),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="narracao-aprovacao-{preview_id[:8]}.mp3"',
        },
    )


@router.post("/production-preview/logo-test")
def generate_production_logo_test_video(
    payload: NarrationLogoTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Render a zero-image test MP4 from the exact preview audio + official channel logo."""
    settings = _settings_for_user(db, current_user.id)
    logo_path = _official_logo_path(settings)
    try:
        return youtube_narration_gate_service.generate_logo_test_video(
            preview_id=payload.preview_id,
            user_id=current_user.id,
            logo_path=logo_path,
        )
    except YouTubeNarrationGateError as exc:
        _raise_gate_error(exc)


@router.get("/production-preview/logo-test/{preview_id}")
def get_production_logo_test_video(
    preview_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        path = youtube_narration_gate_service.logo_test_video_path(
            preview_id=preview_id,
            user_id=current_user.id,
        )
    except YouTubeNarrationGateError as exc:
        _raise_gate_error(exc)
    return FileResponse(
        str(path),
        media_type="video/mp4",
        headers={
            "Cache-Control": "private, no-store",
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="teste-logo-narracao-{preview_id[:8]}.mp4"',
        },
    )
