import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.modules.humor_factory.models import HumorChannel, HumorProject
from app.modules.humor_factory.service import HumorFactoryService


router = APIRouter(prefix="/humor-factory", tags=["humor-factory"])
_service = None

def get_service():
    global _service
    if _service is None:
        _service = HumorFactoryService()
    return _service


class HumorChannelRequest(BaseModel):
    id: Optional[int] = None
    name: str = Field(default="Canal de Humor")
    description: Optional[str] = ""
    avatar_path: Optional[str] = None
    catchphrases: List[str] = Field(default_factory=list)
    default_voice_gender: str = Field(default="male")
    allowed_themes: List[str] = Field(default_factory=list)
    is_active: bool = True


class HumorProjectRequest(BaseModel):
    channel_id: Optional[int] = None
    title: Optional[str] = None
    theme: Optional[str] = None
    themes: List[str] = Field(default_factory=list)
    joke_source: str = Field(default="ai")  # ai | manual | mixed
    manual_jokes_text: Optional[str] = None
    avatar_override_path: Optional[str] = None
    opening_message: Optional[str] = None
    catchphrase_message: Optional[str] = None
    catchphrases: List[str] = Field(default_factory=list)
    closing_message: Optional[str] = None
    target_minutes: int = Field(default=10, ge=1, le=60)
    auto_publish_after_review: bool = False
    start_immediately: bool = True


class HumorReviewRequest(BaseModel):
    notes: Optional[str] = ""
    publish_now: bool = False


def _parse_json_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def _project_to_dict(p: HumorProject) -> Dict[str, Any]:
    jokes = _parse_json_list(p.jokes_json)
    themes = [x.strip() for x in str(p.theme or "").split("|") if x.strip()]
    return {
        "id": p.id,
        "channel_id": p.channel_id,
        "title": p.title,
        "theme": p.theme,
        "themes": themes,
        "joke_source": p.joke_source,
        "manual_jokes_text": p.manual_jokes_text,
        "avatar_override_path": p.avatar_override_path,
        "opening_message": p.opening_message,
        "catchphrase_message": p.catchphrase_message,
        "catchphrases": _parse_json_list(p.catchphrases_json),
        "closing_message": p.closing_message,
        "jokes_count": len(jokes),
        "target_minutes": p.target_minutes,
        "auto_publish_after_review": bool(p.auto_publish_after_review),
        "status": p.status,
        "progress": int(p.progress or 0),
        "status_message": p.status_message,
        "logs": p.logs or "",
        "review_notes": p.review_notes,
        "video_path": p.video_path,
        "scheduled_video_id": p.scheduled_video_id,
        "youtube_video_id": p.youtube_video_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _channel_to_dict(c: HumorChannel) -> Dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "avatar_path": c.avatar_path,
        "catchphrases": _parse_json_list(c.catchphrases_json),
        "default_voice_gender": c.default_voice_gender,
        "allowed_themes": _parse_json_list(c.allowed_themes),
        "is_active": bool(c.is_active),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


@router.get("/channel")
def get_channel(db: Session = Depends(get_db)):
    channel = (
        db.query(HumorChannel)
        .filter(HumorChannel.is_active == True)  # noqa: E712
        .order_by(HumorChannel.id.desc())
        .first()
    )
    if not channel:
        channel = db.query(HumorChannel).order_by(HumorChannel.id.desc()).first()
    return _channel_to_dict(channel) if channel else None


@router.post("/channel")
def upsert_channel(payload: HumorChannelRequest, db: Session = Depends(get_db)):
    channel = None
    if payload.id:
        channel = db.query(HumorChannel).filter(HumorChannel.id == payload.id).first()
    if not channel:
        channel = (
            db.query(HumorChannel)
            .filter(HumorChannel.is_active == True)  # noqa: E712
            .order_by(HumorChannel.id.desc())
            .first()
        )
    if not channel:
        channel = HumorChannel()
        db.add(channel)

    channel.name = (payload.name or "Canal de Humor").strip()
    channel.description = (payload.description or "").strip() or None
    channel.avatar_path = (payload.avatar_path or "").strip() or channel.avatar_path
    channel.catchphrases_json = json.dumps(payload.catchphrases or [], ensure_ascii=False)
    channel.default_voice_gender = (payload.default_voice_gender or "male").strip().lower()
    channel.allowed_themes = json.dumps(payload.allowed_themes or [], ensure_ascii=False)
    channel.is_active = bool(payload.is_active)
    channel.updated_at = datetime.now()
    db.commit()
    db.refresh(channel)
    return _channel_to_dict(channel)


@router.post("/channel/avatar")
async def upload_channel_avatar(file: UploadFile = File(...)):
    target_dir = Path("app/static/generated/humor_avatars")
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in (file.filename or "avatar.png") if ch.isalnum() or ch in {".", "_", "-"}).strip()
    if not safe_name:
        safe_name = "avatar.png"
    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    target_path = target_dir / filename
    content = await file.read()
    with open(target_path, "wb") as f:
        f.write(content)
    return {"avatar_path": str(target_path.absolute()), "public_url": f"/static/generated/humor_avatars/{filename}"}


@router.post("/projects/avatar")
async def upload_project_avatar(file: UploadFile = File(...)):
    target_dir = Path("app/static/generated/humor_project_avatars")
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in (file.filename or "avatar.png") if ch.isalnum() or ch in {".", "_", "-"}).strip()
    if not safe_name:
        safe_name = "avatar.png"
    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    target_path = target_dir / filename
    content = await file.read()
    with open(target_path, "wb") as f:
        f.write(content)
    return {"avatar_path": str(target_path.absolute()), "public_url": f"/static/generated/humor_project_avatars/{filename}"}


@router.post("/projects")
def create_project(payload: HumorProjectRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    source = (payload.joke_source or "ai").strip().lower()
    if source not in {"ai", "manual", "mixed"}:
        source = "ai"
    if source == "manual" and not (payload.manual_jokes_text or "").strip():
        raise HTTPException(status_code=400, detail="Informe as piadas no modo manual.")

    raw_themes = [str(t).strip() for t in (payload.themes or []) if str(t).strip()]
    if payload.theme and str(payload.theme).strip():
        raw_themes.append(str(payload.theme).strip())
    themes = []
    for t in raw_themes:
        if t not in themes:
            themes.append(t)
    if not themes:
        raise HTTPException(status_code=400, detail="Selecione pelo menos um tema de piadas.")

    target_minutes = max(10, int(payload.target_minutes or 10))
    project = HumorProject(
        channel_id=payload.channel_id,
        title=(payload.title or "").strip() or None,
        theme=" | ".join(themes),
        joke_source=source,
        manual_jokes_text=payload.manual_jokes_text or "",
        avatar_override_path=(payload.avatar_override_path or "").strip() or None,
        opening_message=(payload.opening_message or "").strip() or None,
        catchphrase_message=(payload.catchphrase_message or "").strip() or None,
        catchphrases_json=json.dumps(payload.catchphrases or [], ensure_ascii=False),
        closing_message=(payload.closing_message or "").strip() or None,
        target_minutes=target_minutes,
        auto_publish_after_review=bool(payload.auto_publish_after_review),
        status="queued",
        progress=0,
        status_message="Projeto criado. Aguardando geração do vídeo...",
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    if payload.start_immediately:
        project.status = "queued"
        project.status_message = "Projeto salvo e reenfileirado para geração."
        project.updated_at = datetime.now()
        db.commit()
        background_tasks.add_task(get_service().generate_project_video, project.id)
    return _project_to_dict(project)


@router.get("/projects")
def list_projects(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(HumorProject).order_by(HumorProject.id.desc()).limit(max(1, min(500, int(limit)))).all()
    return [_project_to_dict(r) for r in rows]


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    row = db.query(HumorProject).filter(HumorProject.id == project_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    return _project_to_dict(row)


@router.post("/projects/{project_id}/generate")
def regenerate_project(project_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    row = db.query(HumorProject).filter(HumorProject.id == project_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    row.status = "queued"
    row.progress = 0
    row.status_message = "Regeneração solicitada."
    row.updated_at = datetime.now()
    db.commit()
    background_tasks.add_task(get_service().generate_project_video, project_id)
    return {"status": "queued", "message": "Regeneração iniciada em background."}


@router.post("/projects/{project_id}/approve")
def approve_project(project_id: int, payload: HumorReviewRequest, db: Session = Depends(get_db)):
    row = db.query(HumorProject).filter(HumorProject.id == project_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    if row.status not in {"review", "approved"}:
        raise HTTPException(status_code=400, detail="Projeto ainda não está em revisão.")

    row.status = "approved"
    row.review_notes = (payload.notes or "").strip() or row.review_notes
    row.status_message = "Projeto aprovado para publicação."
    row.updated_at = datetime.now()
    db.commit()

    if payload.publish_now or row.auto_publish_after_review:
        try:
            result = get_service().publish_project(project_id)
            return {"status": "published", "result": result}
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
    return _project_to_dict(row)


@router.post("/projects/{project_id}/reject")
def reject_project(project_id: int, payload: HumorReviewRequest, db: Session = Depends(get_db)):
    row = db.query(HumorProject).filter(HumorProject.id == project_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    row.status = "rejected"
    row.review_notes = (payload.notes or "").strip() or "Reprovado na revisão."
    row.status_message = "Projeto reprovado. Ajuste o tema/piadas e gere novamente."
    row.updated_at = datetime.now()
    db.commit()
    return _project_to_dict(row)


@router.post("/projects/{project_id}/publish")
def publish_project(project_id: int):
    try:
        return get_service().publish_project(project_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    row = db.query(HumorProject).filter(HumorProject.id == project_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    if row.video_path and os.path.exists(row.video_path):
        try:
            os.remove(row.video_path)
        except Exception:
            pass
    db.delete(row)
    db.commit()
    return {"status": "deleted"}
