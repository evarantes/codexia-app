"""
Router do Canal de Piadas.
- Temas: português, religiosa, gospel, etc.
- Geração de piadas por IA ou inserção manual
- Vídeo com avatar fixo + TTS
- Revisão e publicação automática
"""
import os
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models import JokesChannel, JokesVideo, Joke
from app.services.jokes_service import JokesService
from app.services.jokes_factory import JokesFactory
from app.services.youtube_service import YouTubeService
from app.config import VIDEO_OUTPUT_DIR, path_from_static_url


router = APIRouter(prefix="/jokes", tags=["Canal de Piadas"])


# --- Schemas ---

class ChannelCreate(BaseModel):
    name: str
    voice_style: str = "human"
    voice_gender: str = "male"


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None
    avatar_image_path: Optional[str] = None
    auto_publish_after_approval: Optional[bool] = None


class GenerateJokesRequest(BaseModel):
    theme: str
    quantity: int = 15
    duration_min: int = 10


class ParseJokesRequest(BaseModel):
    text: str
    theme: str = "geral"


class VideoCreate(BaseModel):
    channel_id: int
    title: Optional[str] = None
    theme: str
    duration_min: int = 10
    jokes: List[dict]  # [{punchline, setup?, source?, theme?}]


class VideoApproveRequest(BaseModel):
    notes: Optional[str] = None
    schedule_for: Optional[str] = None  # ISO datetime
    auto_publish: bool = True


# --- Temas ---

@router.get("/themes")
def get_themes():
    """Lista temas disponíveis para piadas."""
    return JokesService().get_themes()


# --- Geração de piadas ---

@router.post("/generate")
def generate_jokes(request: GenerateJokesRequest):
    """Gera piadas por IA com base no tema."""
    try:
        jokes = JokesService().generate_jokes(
            theme=request.theme,
            quantity=request.quantity,
            duration_min=request.duration_min,
        )
        return {"jokes": jokes}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/parse")
def parse_jokes(request: ParseJokesRequest):
    """Parseia piadas manuais (uma por linha ou separadas por ---)."""
    jokes = JokesService().parse_manual_jokes(request.text, request.theme)
    return {"jokes": jokes}


# --- Canais ---

@router.get("/channels")
def list_channels(db: Session = Depends(get_db)):
    """Lista canais de piadas."""
    channels = db.query(JokesChannel).all()
    return [{"id": c.id, "name": c.name, "voice_style": c.voice_style, "voice_gender": c.voice_gender} for c in channels]


@router.post("/channels")
def create_channel(request: ChannelCreate, db: Session = Depends(get_db)):
    """Cria canal de piadas."""
    channels = db.query(JokesChannel).all()
    if not channels:
        channel = JokesChannel(
            name=request.name,
            voice_style=request.voice_style,
            voice_gender=request.voice_gender,
        )
        db.add(channel)
        db.commit()
        db.refresh(channel)
        return {"id": channel.id, "name": channel.name}
    ch = channels[0]
    ch.name = request.name
    ch.voice_style = request.voice_style
    ch.voice_gender = request.voice_gender
    db.commit()
    return {"id": ch.id, "name": ch.name}


@router.get("/channels/{channel_id}")
def get_channel(channel_id: int, db: Session = Depends(get_db)):
    """Retorna canal."""
    ch = db.query(JokesChannel).filter(JokesChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal não encontrado")
    return {"id": ch.id, "name": ch.name, "voice_style": ch.voice_style, "voice_gender": ch.voice_gender, "avatar_image_path": ch.avatar_image_path}


@router.patch("/channels/{channel_id}")
def update_channel(channel_id: int, request: ChannelUpdate, db: Session = Depends(get_db)):
    """Atualiza canal."""
    ch = db.query(JokesChannel).filter(JokesChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal não encontrado")
    if request.name is not None:
        ch.name = request.name
    if request.voice_style is not None:
        ch.voice_style = request.voice_style
    if request.voice_gender is not None:
        ch.voice_gender = request.voice_gender
    if request.avatar_image_path is not None:
        ch.avatar_image_path = request.avatar_image_path
    if request.auto_publish_after_approval is not None:
        ch.auto_publish_after_approval = request.auto_publish_after_approval
    db.commit()
    return {"id": ch.id, "name": ch.name}


@router.post("/channels/{channel_id}/upload-avatar")
async def upload_avatar(channel_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Faz upload da imagem do avatar."""
    ch = db.query(JokesChannel).filter(JokesChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal não encontrado")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Arquivo deve ser uma imagem (PNG, JPG, etc.)")
    os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)
    filename = f"jokes_avatar_{channel_id}.png"
    path = os.path.join(VIDEO_OUTPUT_DIR, filename)
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    ch.avatar_image_path = path
    db.commit()
    return {"avatar_path": path}


# --- Vídeos ---

@router.get("/videos")
def list_videos(db: Session = Depends(get_db)):
    """Lista vídeos de piadas."""
    videos = db.query(JokesVideo).order_by(JokesVideo.created_at.desc()).all()
    return [
        {
            "id": v.id,
            "channel_id": v.channel_id,
            "title": v.title,
            "theme": v.theme,
            "status": v.status,
            "video_path": v.video_path,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "jokes_count": len(v.jokes) if v.jokes else 0,
        }
        for v in videos
    ]


@router.post("/videos")
def create_video(request: VideoCreate, db: Session = Depends(get_db)):
    """Cria vídeo de piadas com lista de piadas."""
    ch = db.query(JokesChannel).filter(JokesChannel.id == request.channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal não encontrado")
    video = JokesVideo(
        channel_id=request.channel_id,
        title=request.title or f"Piadas {request.theme}",
        theme=request.theme,
        duration_min=request.duration_min,
        status="draft",
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    for i, j in enumerate(request.jokes):
        joke = Joke(
            video_id=video.id,
            idx=i + 1,
            punchline=j.get("punchline", ""),
            setup=j.get("setup"),
            source=j.get("source", "ai"),
            theme=j.get("theme", request.theme),
        )
        db.add(joke)
    db.commit()
    return {"id": video.id, "title": video.title, "status": video.status}


@router.get("/videos/{video_id}")
def get_video(video_id: int, db: Session = Depends(get_db)):
    """Retorna vídeo com piadas."""
    v = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    return {
        "id": v.id,
        "channel_id": v.channel_id,
        "title": v.title,
        "theme": v.theme,
        "status": v.status,
        "video_path": v.video_path,
        "youtube_video_id": v.youtube_video_id,
        "review_notes": v.review_notes,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "jokes": [{"id": j.id, "idx": j.idx, "punchline": j.punchline, "setup": j.setup, "source": j.source} for j in v.jokes],
    }


def _run_jokes_generation(video_id: int):
    """Tarefa em background para gerar vídeo."""
    db = SessionLocal()
    try:
        video = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
        if not video:
            return
        video.status = "generating"
        db.commit()
        factory = JokesFactory(db)
        def progress_cb(p, msg):
            pass  # TODO: opcional websocket/SSE
        path = factory.generate_video(video, progress_callback=progress_cb)
        video.video_path = path
        video.status = "pending_review"
        db.commit()
    except Exception as e:
        video = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
        if video:
            video.status = "draft"
            video.review_notes = f"Erro na geração: {e}"
            db.commit()
    finally:
        db.close()


@router.post("/videos/{video_id}/generate")
def generate_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Inicia geração do vídeo."""
    v = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    if v.status == "generating":
        raise HTTPException(status_code=400, detail="Vídeo já está sendo gerado")
    if not v.jokes:
        raise HTTPException(status_code=400, detail="Adicione piadas antes de gerar")
    background_tasks.add_task(_run_jokes_generation, video_id)
    return {"status": "generating", "message": "Geração iniciada em background"}


@router.post("/videos/{video_id}/approve")
def approve_video(video_id: int, request: VideoApproveRequest, db: Session = Depends(get_db)):
    """Aprova vídeo para publicação."""
    v = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    if v.status != "pending_review":
        raise HTTPException(status_code=400, detail="Vídeo já foi aprovado ou rejeitado")
    v.status = "approved"
    v.review_notes = request.notes
    if request.schedule_for:
        try:
            v.scheduled_for = datetime.fromisoformat(request.schedule_for.replace("Z", "+00:00"))
        except Exception:
            pass
    db.commit()
    return {"status": "approved", "message": "Vídeo aprovado. Pode publicar agora."}


@router.post("/videos/{video_id}/reject")
def reject_video(video_id: int, notes: Optional[str] = None, db: Session = Depends(get_db)):
    """Rejeita vídeo."""
    v = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    v.status = "rejected"
    v.review_notes = notes
    db.commit()
    return {"status": "rejected"}


def _resolve_video_path(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    name = os.path.basename(raw.strip())
    for d in (VIDEO_OUTPUT_DIR, str(path_from_static_url("videos"))):
        if d:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return raw if os.path.isfile(raw) else None


@router.post("/videos/{video_id}/publish")
def publish_video(video_id: int, db: Session = Depends(get_db)):
    """Publica vídeo no YouTube."""
    v = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    if v.status not in ("approved", "pending_review"):
        raise HTTPException(status_code=400, detail="Aprove o vídeo antes de publicar")
    path = _resolve_video_path(v.video_path)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=400, detail="Arquivo de vídeo não encontrado")
    yt = YouTubeService()
    title = v.title or f"Piadas {v.theme}"
    tags = ["piadas", "humor", v.theme or "geral"]
    upload_result = yt.upload_video(path, title=title, description=f"Vídeo de piadas - Tema: {v.theme}", tags=tags)
    if isinstance(upload_result, dict) and upload_result.get("error"):
        raise HTTPException(status_code=502, detail=upload_result.get("error", "Erro ao publicar no YouTube"))
    youtube_id = upload_result.get("id") if isinstance(upload_result, dict) else getattr(upload_result, "id", None)
    v.status = "published"
    v.youtube_video_id = youtube_id
    v.published_at = datetime.now()
    db.commit()
    return {"status": "published", "youtube_video_id": youtube_id}


@router.get("/videos/{video_id}/download")
def download_video(video_id: int, db: Session = Depends(get_db)):
    """Baixa o vídeo gerado."""
    v = db.query(JokesVideo).filter(JokesVideo.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    path = _resolve_video_path(v.video_path)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(path, media_type="video/mp4", filename=f"jokes_{video_id}.mp4")
