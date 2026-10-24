"""Authenticated handoff: approved brief -> existing Codexia music pipeline.

Requires the regular Codexia user session; bridge bearer token alone cannot
spend provider credits. No publishing or automatic clip generation.
"""
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from app.models import User
from app.routers.auth import get_current_user
from app.routers.music import GenerateMusicRequest, generate_music_from_lyrics
from app.services.suno_service import get_suno_api_key
from assistant_bridge.router import database, now
from assistant_bridge.codexia_adapter import music_payload

router = APIRouter(prefix="/api/assistant-production", tags=["assistant-production"])


@router.post("/projects/{project_id}/start-music")
def start_music(project_id: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """One-shot, user-confirmed start. Fail closed without Suno configuration."""
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
