"""Entradas genéricas de vídeo delegadas ao pipeline História/Devocional."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Book
from app.services.unified_video_pipeline import (
    build_unified_video_request,
    unified_video_pipeline,
)


router = APIRouter(prefix="/video", tags=["Video"])


class VideoRequest(BaseModel):
    title: str
    script: List[str]


class AutoVideoRequest(BaseModel):
    book_id: int
    style: str = "drama"


class CreateVideoRequest(BaseModel):
    mode: str = "manual"
    title: str
    content: str
    duration: int = 1
    voice_style: Optional[str] = "human"
    voice_gender: Optional[str] = "female"
    storyboard_quantity: int = 15
    storyboard_images: Optional[List[str]] = None


def _pipeline_response(result) -> Dict[str, Any]:
    return {
        "message": result.message,
        "task_id": result.task_id,
        "unified_video_id": result.unified_video_id,
        "status": result.status,
        "queued": bool(result.queue_position > 1 or result.already_processing),
        "queue_position": int(result.queue_position or 0),
        "reused_existing_task": bool(result.reused_existing),
        "reused_completed_task": bool(result.reused_completed),
        "video_url": result.video_url,
        "pipeline": "unified_video_pipeline",
    }


def _submit(db: Session, payload: Dict[str, Any], *, source_id: Optional[str] = None) -> Dict[str, Any]:
    request = build_unified_video_request(
        payload,
        source_module="video_router",
        source_id=source_id,
        user_id=None,
    )
    try:
        from app.routers.youtube import _kick_story_video_task_queue_async

        kick = _kick_story_video_task_queue_async if callable(_kick_story_video_task_queue_async) else None
    except Exception:
        kick = None
    result = unified_video_pipeline().submit_or_reuse(
        db,
        request=request,
        kick_queue_callback=kick,
        legacy_initial_result={
            "source_module": "video_router",
            "pipeline": "unified_video_pipeline",
            "payload": payload,
        },
        user=None,
    )
    return _pipeline_response(result)


@router.post("/create")
def create_video(request: CreateVideoRequest, db: Session = Depends(get_db)):
    mode = str(request.mode or "manual").strip().lower()
    if mode not in {"manual", "topic", "story", "short"}:
        mode = "topic"
    selected_images = [
        str(value).strip()
        for value in (request.storyboard_images or [])
        if str(value or "").strip()
    ]
    script_lines = [line.strip() for line in str(request.content or "").splitlines() if line.strip()]
    seeded_script = None
    if mode == "manual":
        seeded_script = {
            "title": request.title,
            "scenes": [{"text": line} for line in script_lines],
        }
        if selected_images:
            seeded_script["selected_images"] = selected_images
    payload = {
        "topic": request.title or request.content,
        "story_content": request.content if mode in {"manual", "story"} else None,
        "mode": "story" if mode in {"manual", "story"} else "topic",
        "kind": "short" if mode == "short" else ("story" if mode == "story" else "custom"),
        "duration": request.duration,
        "aspect_ratio": "9:16" if mode == "short" else "16:9",
        "voice_style": request.voice_style,
        "voice_gender": request.voice_gender,
        "image_count": request.storyboard_quantity,
        "selected_images": selected_images or None,
        "seeded_script": seeded_script,
        "override_title": request.title,
        "review_required": True,
    }
    try:
        return _submit(db, payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Falha no pipeline canônico: {type(exc).__name__}: {str(exc)[:300]}")


@router.post("/generate")
def generate_video(request: VideoRequest, db: Session = Depends(get_db)):
    script = [str(line).strip() for line in request.script if str(line or "").strip()]
    if not script:
        raise HTTPException(status_code=422, detail="script deve conter ao menos uma linha.")
    payload = {
        "topic": request.title,
        "story_content": "\n\n".join(script),
        "mode": "story",
        "kind": "custom",
        "duration": max(1, min(180, len(" ".join(script).split()) // 130 or 1)),
        "aspect_ratio": "16:9",
        "seeded_script": {
            "title": request.title,
            "scenes": [{"text": line} for line in script],
        },
        "review_required": True,
    }
    try:
        return _submit(db, payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Falha no pipeline canônico: {type(exc).__name__}: {str(exc)[:300]}")


@router.post("/generate-auto")
def generate_auto_video(request: AutoVideoRequest, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == request.book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    payload = {
        "topic": book.title,
        "story_content": book.synopsis,
        "mode": "story",
        "kind": "story",
        "duration": 5,
        "aspect_ratio": "16:9",
        "override_title": book.title,
        "legacy_book_style": request.style,
        "cover_image_url": book.cover_image_url,
        "review_required": True,
    }
    try:
        return _submit(db, payload, source_id=f"book:{int(book.id)}:{request.style}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Falha no pipeline canônico: {type(exc).__name__}: {str(exc)[:300]}")
