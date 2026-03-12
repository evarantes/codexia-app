import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ScheduledVideo


router = APIRouter(prefix="/jokes", tags=["jokes"])


DEFAULT_JOKE_THEMES = [
    "Português",
    "Religiosa",
    "Gospel",
    "Família",
    "Escola",
    "Trabalho",
    "Casal",
    "Amizade",
]

DEFAULT_AVATAR_PROMPT = (
    "Friendly Brazilian comedy host avatar, clean humor style, medium shot, "
    "warm studio lights, expressive face, slight mouth movement, "
    "family friendly, no text, no watermark"
)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw)
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _normalize_video_url(raw_url: Optional[str]) -> Optional[str]:
    if not raw_url:
        return raw_url
    value = str(raw_url).strip()
    if value.startswith(("http://", "https://", "/media/videos/", "/static/videos/")):
        return value
    filename = value.split("/")[-1]
    if filename:
        return f"/media/videos/{filename}"
    return value


def _is_jokes_video(video: ScheduledVideo) -> bool:
    script_data = (video.script_data or "").lower()
    return '"mode": "jokes_channel"' in script_data or '"mode":"jokes_channel"' in script_data


def _extract_manual_jokes(manual_jokes_text: Optional[str], manual_jokes: Optional[List[str]]) -> List[str]:
    lines: List[str] = []
    if manual_jokes:
        lines.extend([str(item).strip() for item in manual_jokes if str(item).strip()])
    if manual_jokes_text:
        for raw in str(manual_jokes_text).splitlines():
            clean = raw.strip().lstrip("-").strip()
            if clean:
                lines.append(clean)
    # Remover duplicadas mantendo ordem
    seen = set()
    result: List[str] = []
    for item in lines:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


class JokeQueueRequest(BaseModel):
    channel_name: str = "Canal de Piadas Livres"
    theme: str = "Português"
    source: str = Field(default="ai", pattern="^(ai|manual)$")
    manual_jokes_text: Optional[str] = None
    manual_jokes: Optional[List[str]] = None
    avatar_name: str = "Tio da Risada"
    avatar_prompt: str = DEFAULT_AVATAR_PROMPT
    video_minutes: int = 10
    videos_count: int = 1
    jokes_per_video: Optional[int] = None
    start_at: Optional[str] = None
    interval_minutes: int = 1440
    auto_post_after_review: bool = False


class JokeApprovalRequest(BaseModel):
    publish_now: bool = False
    publish_at: Optional[str] = None


@router.get("/themes")
def get_joke_themes():
    return {"themes": DEFAULT_JOKE_THEMES}


@router.post("/queue")
def create_jokes_queue(
    request: JokeQueueRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    source = (request.source or "ai").strip().lower()
    if source not in {"ai", "manual"}:
        raise HTTPException(status_code=400, detail="source deve ser 'ai' ou 'manual'.")

    manual_jokes = _extract_manual_jokes(request.manual_jokes_text, request.manual_jokes)
    if source == "manual" and not manual_jokes:
        raise HTTPException(
            status_code=400,
            detail="No modo manual, informe ao menos uma piada para montar o roteiro.",
        )

    minutes = max(10, int(request.video_minutes or 10))
    videos_count = max(1, min(30, int(request.videos_count or 1)))
    interval_minutes = max(1, int(request.interval_minutes or 1440))
    jokes_per_video = request.jokes_per_video
    if jokes_per_video is None:
        # Aproximação: ~15s por piada -> >=40 para 10 minutos.
        jokes_per_video = max(20, int((minutes * 60) / 15))
    else:
        jokes_per_video = max(10, int(jokes_per_video))

    first_schedule = _parse_datetime(request.start_at) or datetime.now()

    created: List[ScheduledVideo] = []
    for idx in range(videos_count):
        scheduled_for = first_schedule + timedelta(minutes=idx * interval_minutes)
        title = f"{request.channel_name} • {request.theme} • Ep. {idx + 1}"
        desc = (
            f"Vídeo de piadas limpas para revisão. "
            f"Tema: {request.theme}. Duração alvo: {minutes}+ min."
        )
        payload: Dict[str, Any] = {
            "mode": "jokes_channel",
            "channel_name": request.channel_name,
            "joke_theme": request.theme,
            "jokes_source": source,
            "manual_jokes": manual_jokes if source == "manual" else [],
            "video_duration_minutes": minutes,
            "jokes_per_video": jokes_per_video,
            "avatar_name": request.avatar_name,
            "avatar_prompt": request.avatar_prompt or DEFAULT_AVATAR_PROMPT,
            "review_required": True,
            "clean_humor_only": True,
            "auto_post_after_review": bool(request.auto_post_after_review),
            "episode_number": idx + 1,
        }

        item = ScheduledVideo(
            theme=request.theme,
            title=title,
            description=desc,
            scheduled_for=scheduled_for,
            status="queued",
            video_type="video",
            script_data=json.dumps(payload, ensure_ascii=False),
            auto_post=False,  # Sempre exige revisão humana antes de habilitar auto publicação
            voice_style="human",
            voice_gender="male",
        )
        db.add(item)
        db.flush()
        created.append(item)

    db.commit()

    if created:
        # Tenta iniciar o primeiro imediatamente se a fila estiver livre.
        processing = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").first()
        if not processing:
            from app.services.video_processing import process_scheduled_video

            background_tasks.add_task(process_scheduled_video, created[0].id)

    return {
        "status": "ok",
        "message": f"{len(created)} vídeo(s) de piadas enfileirado(s) para produção.",
        "videos": [
            {
                "id": v.id,
                "title": v.title,
                "status": v.status,
                "scheduled_for": v.scheduled_for.isoformat() if v.scheduled_for else None,
            }
            for v in created
        ],
    }


@router.get("/review-queue")
def get_jokes_review_queue(db: Session = Depends(get_db)):
    videos = (
        db.query(ScheduledVideo)
        .filter(ScheduledVideo.script_data.isnot(None))
        .filter(ScheduledVideo.script_data.contains("jokes_channel"))
        .order_by(ScheduledVideo.id.desc())
        .all()
    )
    result: List[Dict[str, Any]] = []
    for v in videos:
        result.append(
            {
                "id": v.id,
                "theme": v.theme,
                "title": v.title,
                "description": v.description,
                "status": v.status,
                "progress": v.progress or 0,
                "scheduled_for": v.scheduled_for.isoformat() if v.scheduled_for else None,
                "auto_post": bool(v.auto_post),
                "video_url": _normalize_video_url(v.video_url),
                "youtube_video_id": v.youtube_video_id,
                "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
            }
        )
    return result


@router.post("/review/{video_id}/approve")
def approve_jokes_video(
    video_id: int,
    request: JokeApprovalRequest,
    db: Session = Depends(get_db),
):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video or not _is_jokes_video(video):
        raise HTTPException(status_code=404, detail="Vídeo de piadas não encontrado.")

    publish_at = _parse_datetime(request.publish_at)
    if request.publish_now:
        video.scheduled_for = datetime.now()
    elif publish_at:
        video.scheduled_for = publish_at

    # Caso legado com status customizado, normaliza para completed
    if (video.status or "").strip().lower() in {"awaiting_publish", "ready"}:
        video.status = "completed"

    video.auto_post = True
    db.commit()

    return {
        "status": "approved",
        "message": "Vídeo aprovado para publicação automática.",
        "video": {
            "id": video.id,
            "status": video.status,
            "auto_post": video.auto_post,
            "scheduled_for": video.scheduled_for.isoformat() if video.scheduled_for else None,
        },
    }


@router.post("/review/{video_id}/regenerate")
def regenerate_jokes_video(
    video_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video or not _is_jokes_video(video):
        raise HTTPException(status_code=404, detail="Vídeo de piadas não encontrado.")

    script_data = {}
    try:
        script_data = json.loads(video.script_data or "{}")
    except Exception:
        script_data = {}

    # Força geração nova de cenas/piadas na próxima execução
    for key in ("scenes", "title", "description", "tags"):
        script_data.pop(key, None)
    video.script_data = json.dumps(script_data, ensure_ascii=False)

    video.video_url = None
    video.youtube_video_id = None
    video.uploaded_at = None
    video.status = "queued"
    video.progress = 0
    video.auto_post = False
    db.commit()

    processing = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").first()
    if not processing:
        from app.services.video_processing import process_scheduled_video

        background_tasks.add_task(process_scheduled_video, video.id)

    return {"status": "queued", "message": "Vídeo reenfileirado para regeneração."}
