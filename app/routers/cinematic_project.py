from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.models import User
from app.routers.auth import get_current_admin_user
from app.routers.cinematic_campaign import _user_id
from app.services.cinematic_director_job_store import DirectorJobStore
from app.services.cinematic_project_store import CinematicProjectStore


router = APIRouter(prefix="/cinematic", tags=["Codexia Cinematic Project"])
_projects = CinematicProjectStore()
_director_jobs = DirectorJobStore()


class ActiveProjectSave(BaseModel):
    theme: Optional[str] = Field(None, max_length=500)
    content_type: Optional[str] = Field(None, pattern="^(story|devotional|short)$")
    duration_minutes: Optional[int] = Field(None, ge=1, le=30)
    budget_brl: Optional[float] = Field(None, ge=1, le=1000)
    plan: Optional[Dict[str, Any]] = None
    director: Optional[Dict[str, Any]] = None
    estimate: Optional[Dict[str, Any]] = None
    base_task_id: Optional[str] = Field(None, max_length=160)
    status: Optional[str] = Field(None, max_length=80)


class SceneStateSave(BaseModel):
    provider: Optional[str] = Field(None, max_length=40)
    requested_provider: Optional[str] = Field(None, max_length=40)
    job_id: Optional[str] = Field(None, max_length=600)
    status: Optional[str] = Field(None, max_length=80)
    output_url: Optional[str] = Field(None, max_length=2000)
    filename: Optional[str] = Field(None, max_length=500)
    prompt: Optional[str] = Field(None, max_length=12000)
    approved: Optional[bool] = None
    error: Optional[str] = Field(None, max_length=2000)


def _recover_from_latest_director(user_id: int) -> Optional[Dict[str, Any]]:
    latest = _director_jobs.latest_completed(user_id)
    if not latest:
        return None
    result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
    request = latest.get("request") if isinstance(latest.get("request"), dict) else {}
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else None
    if not plan:
        return None
    return _projects.write(
        user_id,
        {
            "theme": request.get("theme") or plan.get("theme") or "",
            "content_type": request.get("content_type") or plan.get("content_type") or "story",
            "duration_minutes": request.get("duration_minutes") or plan.get("duration_minutes") or 10,
            "budget_brl": request.get("budget_brl") or plan.get("budget_limit_brl") or 90,
            "plan": plan,
            "director": result.get("director") if isinstance(result.get("director"), dict) else {},
            "status": "directed",
            "recovered_from_director_job": latest.get("job_id"),
        },
    )


@router.get("/project/active")
def get_active_project(
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    project = _projects.read(uid)
    recovered = False
    if project is None:
        project = _recover_from_latest_director(uid)
        recovered = project is not None
    return {"project": project, "recovered": recovered}


@router.put("/project/active")
def save_active_project(
    body: ActiveProjectSave,
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"project": _projects.write(uid, payload)}


@router.put("/project/active/scenes/{scene_index}")
def save_scene_state(
    scene_index: int,
    body: SceneStateSave,
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"project": _projects.update_scene(uid, scene_index, payload)}


@router.delete("/project/active")
def clear_active_project(
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    return {"cleared": _projects.clear(uid)}
