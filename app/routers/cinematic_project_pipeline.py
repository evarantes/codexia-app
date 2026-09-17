from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_admin_user
from app.routers.cinematic_campaign import _settings, _user_id
from app.services.cinematic_project_store import CinematicProjectStore
from app.services.cinematic_video_provider import CinematicProviderError, CinematicVideoProvider


router = APIRouter(prefix="/cinematic", tags=["Codexia Cinematic Project Pipeline"])
_projects = CinematicProjectStore()


class ProjectSceneSubmit(BaseModel):
    scene_index: int = Field(..., ge=1, le=500)
    provider: str = Field("veo", pattern="^(runway|veo|kling)$")
    prompt: str = Field(..., min_length=3, max_length=12000)
    image_uri: Optional[str] = None
    image_base64: Optional[str] = None
    duration_seconds: int = Field(5, ge=3, le=15)
    premium: bool = False
    aspect_ratio: str = Field("16:9", pattern="^(16:9|9:16)$")
    project_slot: str = Field("story", pattern="^(story|devotional|short)$")


class ProjectScenePoll(BaseModel):
    scene_index: int = Field(..., ge=1, le=500)
    provider: str = Field(..., pattern="^(runway|veo|kling)$")
    job_id: str = Field(..., min_length=3, max_length=600)
    status_url: Optional[str] = None
    response_url: Optional[str] = None
    model_path: Optional[str] = None
    project_slot: str = Field("story", pattern="^(story|devotional|short)$")


@router.post("/project/scene/submit")
def submit_project_scene(
    body: ProjectSceneSubmit,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    provider = CinematicVideoProvider(_settings(db, uid))
    try:
        result = provider.submit(
            provider=body.provider,
            prompt=body.prompt,
            image_uri=body.image_uri,
            image_base64=body.image_base64,
            duration=body.duration_seconds,
            premium=body.premium,
            aspect_ratio=body.aspect_ratio,
        )
    except CinematicProviderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    actual_provider = str(result.get("provider") or body.provider)
    _projects.update_scene(uid, body.scene_index, {
        "provider": actual_provider,
        "requested_provider": body.provider,
        "job_id": result.get("job_id"),
        "status": result.get("status") or "PENDING",
        "status_url": result.get("status_url"),
        "response_url": result.get("response_url"),
        "model_path": result.get("model_path"),
        "prompt": body.prompt,
        "duration_seconds": body.duration_seconds,
        "premium": bool(body.premium),
        "approved": False,
        "error": None,
        "output_url": None,
        "filename": None,
    }, slot=body.project_slot)
    return {**result, "project_slot": body.project_slot}


@router.post("/project/scene/poll")
def poll_project_scene(
    body: ProjectScenePoll,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    provider = CinematicVideoProvider(_settings(db, uid))
    try:
        if body.provider == "runway":
            result = provider.poll_runway(body.job_id)
        elif body.provider == "veo":
            result = provider.poll_veo(body.job_id)
        else:
            result = provider.poll_kling(
                job_id=body.job_id,
                status_url=body.status_url,
                response_url=body.response_url,
                model_path=body.model_path,
            )
    except CinematicProviderError as exc:
        _projects.update_scene(uid, body.scene_index, {"status": "FAILED", "error": str(exc)}, slot=body.project_slot)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    update = {
        "provider": str(result.get("provider") or body.provider),
        "job_id": body.job_id,
        "status": result.get("status") or "PENDING",
        "output_url": result.get("output_url"),
        "filename": result.get("filename"),
        "error": result.get("error"),
    }
    _projects.update_scene(uid, body.scene_index, {k: v for k, v in update.items() if v is not None}, slot=body.project_slot)
    return {**result, "project_slot": body.project_slot}
