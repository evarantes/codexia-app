"""Authenticated handoff: approved brief -> existing Codexia music pipeline.

Requires the regular Codexia user session; bridge bearer token alone cannot
spend provider credits. No publishing or automatic clip generation.
"""
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from app.models import User
from app.routers.auth import get_current_user
from app.routers.music import (GenerateMusicRequest, GenerateClipRequest,
                               generate_music_from_lyrics, generate_music_clip_task)
from app.services.task_manager import get_task
from app.database import get_db
from sqlalchemy.orm import Session
from assistant_bridge.codexia_adapter import clip_payload
from app.services.suno_service import get_suno_api_key
from assistant_bridge.router import database, now, ProductionRequest
from assistant_bridge.codexia_adapter import music_payload

router = APIRouter(prefix="/api/assistant-production", tags=["assistant-production"])


@router.post("/projects", status_code=201)
def create_owned_project(request: ProductionRequest,
                         user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Create a private draft bound to the authenticated Codexia account."""
    project_id = str(uuid.uuid4())
    stamp = now()
    brief = request.model_dump()
    brief["_production"] = {"user_id": user.id}
    with database() as db:
        db.execute(
            "INSERT INTO assistant_projects VALUES (?, ?, ?, ?, ?)",
            (project_id, stamp, stamp, "awaiting_review", json.dumps(brief)),
        )
    return {"id": project_id, "status": "awaiting_review"}


@router.post("/projects/{project_id}/approve")
def approve_owned_project(project_id: str,
                          user: User = Depends(get_current_user)) -> dict[str, str]:
    """Only the owner may approve their own draft; no paid work starts here."""
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        if (json.loads(row["payload"]).get("_production") or {}).get("user_id") != user.id:
            raise HTTPException(403, "Project belongs to another user")
        if row["status"] != "awaiting_review":
            raise HTTPException(409, "Project is not awaiting review")
        db.execute(
            "UPDATE assistant_projects SET status='approved_for_generation', updated_at=? WHERE id=?",
            (now(), project_id),
        )
    return {"id": project_id, "status": "approved_for_generation"}


@router.post("/projects/{project_id}/start-music")
def start_music(project_id: str, user: User = Depends(get_current_user), confirmed: bool = False) -> dict[str, Any]:
    """One-shot, user-confirmed start. Fail closed without Suno configuration."""
    if not confirmed:
        raise HTTPException(428, "Confirm music generation explicitly")
    if not get_suno_api_key():
        raise HTTPException(503, "Suno API Key required for vocal production")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] != "approved_for_generation":
            raise HTTPException(409, "Project is not approved or already started")
        existing_owner = (json.loads(row["payload"]).get("_production") or {}).get("user_id")
        if existing_owner != user.id:
            raise HTTPException(403, "Project must be assigned to the current user before generation")
        db.execute(
            "UPDATE assistant_projects SET status='music_starting', updated_at=? WHERE id=?",
            (now(), project_id),
        )
        db.commit()
    try:
        brief = json.loads(row["payload"])
        request = GenerateMusicRequest(**music_payload(brief))
        result = generate_music_from_lyrics(request, user=user)
    except Exception:
        with database() as db:
            db.execute(
                "UPDATE assistant_projects SET status='music_start_failed', updated_at=? "
                "WHERE id=? AND status='music_starting'", (now(), project_id)
            )
        raise
    with database() as db:
        db.execute(
            "UPDATE assistant_projects SET status='music_processing', updated_at=?, payload=? "
            "WHERE id=? AND status='music_starting'",
            (now(), json.dumps({**brief, "_production": {
                "task_id": result.get("task_id"), "user_id": user.id
            }}), project_id),
        )
    return {"id": project_id, "status": "music_processing", "task_id": result.get("task_id"),
            "auto_upload_youtube": False}


@router.get("/projects/{project_id}/music-status")
def music_status(project_id: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Read owned music job and synchronize its state without starting new work."""
    with database() as db:
        row = db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    brief = json.loads(row["payload"])
    production = brief.get("_production") or {}
    if production.get("user_id") != user.id:
        raise HTTPException(403, "Project is not owned by current user")
    task_id = production.get("task_id")
    if not task_id:
        raise HTTPException(409, "No music task attached")
    # Verify the persisted task owner, not only the bridge payload.
    from app.database import SessionLocal
    from app.models import VideoTask
    with SessionLocal() as owner_db:
        owned = owner_db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if owned is None or owned.user_id != user.id:
            raise HTTPException(403, "Music task ownership could not be verified")
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Music task not found")
    status = task.get("status")
    result = task.get("result") or {}
    filename = result.get("music_filename") if isinstance(result, dict) else None
    if status == "completed" and filename:
        next_status = "music_ready"
        production["music_filename"] = filename
    elif status in ("failed", "cancelled"):
        next_status = "music_failed"
    else:
        next_status = "music_processing"
    brief["_production"] = production
    with database() as db:
        db.execute(
            "UPDATE assistant_projects SET status=?, updated_at=?, payload=? "
            "WHERE id=? AND status IN ('music_processing','music_ready','music_failed')",
            (next_status, now(), json.dumps(brief), project_id),
        )
    effective_status = next_status if row["status"] in ("music_processing", "music_ready", "music_failed") else row["status"]
    return {"id": project_id, "status": effective_status,
            "task_id": task_id, "task_status": status,
            "music_filename": filename if next_status == "music_ready" else None}


@router.post("/projects/{project_id}/start-clip")
def start_clip(project_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    """Explicit second confirmation: start clip only after owned music completes."""
    with database() as bridge_db:
        bridge_db.execute("BEGIN IMMEDIATE")
        row = bridge_db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        brief = json.loads(row["payload"])
        production = brief.get("_production") or {}
        if production.get("user_id") != user.id:
            raise HTTPException(403, "Project is not owned by current user")
        if row["status"] != "music_ready":
            raise HTTPException(409, "Music must be ready and clip not previously started")
        filename = production.get("music_filename")
        if not filename:
            raise HTTPException(409, "Music file is missing")
        bridge_db.execute(
            "UPDATE assistant_projects SET status='clip_starting', updated_at=? WHERE id=?",
            (now(), project_id),
        )
        bridge_db.commit()
    try:
        clip_request = GenerateClipRequest(**clip_payload(brief, filename))
        result = generate_music_clip_task(clip_request, user=user, db=db)
    except Exception:
        with database() as bridge_db:
            bridge_db.execute(
                "UPDATE assistant_projects SET status='clip_start_failed', updated_at=? "
                "WHERE id=? AND status='clip_starting'", (now(), project_id)
            )
        raise
    production["clip_task_id"] = result.get("task_id")
    brief["_production"] = production
    with database() as bridge_db:
        bridge_db.execute(
            "UPDATE assistant_projects SET status='clip_processing', updated_at=?, payload=? "
            "WHERE id=? AND status='clip_starting'",
            (now(), json.dumps(brief), project_id),
        )
    return {"id": project_id, "status": "clip_processing",
            "task_id": result.get("task_id"), "auto_upload_youtube": False}


@router.get("/projects/{project_id}/clip-status")
def clip_status(project_id: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Read the owned clip task and return its deliverable for manual review."""
    with database() as bridge_db:
        row = bridge_db.execute(
            "SELECT status, payload FROM assistant_projects WHERE id=?", (project_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    brief = json.loads(row["payload"])
    production = brief.get("_production") or {}
    if production.get("user_id") != user.id:
        raise HTTPException(403, "Project is not owned by current user")
    task_id = production.get("clip_task_id")
    if not task_id:
        raise HTTPException(409, "No clip task attached")
    # Verify the persisted task owner, not only the bridge payload.
    from app.database import SessionLocal
    from app.models import VideoTask
    with SessionLocal() as owner_db:
        owned = owner_db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if owned is None or owned.user_id != user.id:
            raise HTTPException(403, "Clip task ownership could not be verified")
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Clip task not found")
    task_state = task.get("status")
    if task_state == "completed":
        next_status = "awaiting_clip_review"
    elif task_state in ("failed", "cancelled"):
        next_status = "clip_failed"
    else:
        next_status = "clip_processing"
    with database() as bridge_db:
        bridge_db.execute(
            "UPDATE assistant_projects SET status=?, updated_at=? "
            "WHERE id=? AND status IN ('clip_processing','awaiting_clip_review','clip_failed')",
            (next_status, now(), project_id),
        )
    return {
        "id": project_id,
        "status": next_status,
        "task_id": task_id,
        "task_status": task_state,
        "progress": task.get("progress"),
        "message": task.get("message"),
        "result": task.get("result") if next_status == "awaiting_clip_review" else None,
        "requires_review_before_publication": True,
        "auto_upload_youtube": False,
    }
