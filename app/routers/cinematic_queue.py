from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, VideoTask
from app.routers.auth import get_current_admin_user


router = APIRouter(prefix="/cinematic", tags=["Codexia Cinematic Queue"])


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _result_obj(row: VideoTask) -> Dict[str, Any]:
    raw = getattr(row, "result_json", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _payload(result: Dict[str, Any]) -> Dict[str, Any]:
    value = result.get("payload")
    return value if isinstance(value, dict) else {}


def _title(row: VideoTask, result: Dict[str, Any], payload: Dict[str, Any]) -> str:
    for value in (
        payload.get("override_title"),
        payload.get("topic"),
        result.get("title_hint"),
    ):
        text = str(value or "").strip()
        if text:
            return text[:180]

    story = str(payload.get("story_content") or "").strip()
    if story:
        first = next((line.strip() for line in story.splitlines() if line.strip()), "")
        if first:
            return first[:180]

    message = str(getattr(row, "message", "") or "").strip()
    return message[:180] if message else "Produção sem título"


def _task_to_public(row: VideoTask) -> Dict[str, Any]:
    result = _result_obj(row)
    payload = _payload(result)
    status = str(getattr(row, "status", "") or "pending").strip().lower()
    try:
        progress = int(getattr(row, "progress", 0) or 0)
    except Exception:
        progress = 0
    progress = max(0, min(100, progress))

    message = str(getattr(row, "message", "") or "").strip()
    stage = str(
        getattr(row, "stage", "")
        or result.get("stage")
        or result.get("current_stage")
        or ""
    ).strip()

    error = str(
        result.get("error")
        or result.get("last_error")
        or (message if status == "failed" else "")
        or ""
    ).strip()

    duration = payload.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except Exception:
        duration = None

    kind = str(payload.get("kind") or result.get("kind") or "").strip().lower()

    return {
        "id": str(getattr(row, "id", "") or ""),
        "title": _title(row, result, payload),
        "status": status,
        "progress": progress,
        "stage": stage,
        "message": message,
        "error": error,
        "kind": kind,
        "duration_minutes": duration,
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "can_retry": status in {"failed", "paused"},
        "can_pause": status in {"pending", "processing"},
        "can_cancel": status in {"pending", "processing", "pause_requested", "paused"},
    }


@router.get("/queue")
def get_operational_queue(
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    """Return recent canonical video tasks for the V2 Fila & Custos screen.

    The old YouTube Auto page remains the canonical executor, but V2 can now
    observe the same VideoTask rows directly without navigating to the legacy UI.
    """
    uid = int(getattr(current_user, "id", 0) or 0)
    query = db.query(VideoTask)
    if uid > 0:
        # Some older canonical tasks were created before user_id became mandatory,
        # so include user-less rows only on this admin-authenticated endpoint.
        query = query.filter(or_(VideoTask.user_id == uid, VideoTask.user_id.is_(None)))

    rows = (
        query
        .order_by(VideoTask.updated_at.desc().nullslast(), VideoTask.created_at.desc().nullslast())
        .limit(int(limit))
        .all()
    )
    tasks = [_task_to_public(row) for row in rows]
    counts: Dict[str, int] = {}
    for task in tasks:
        status = task["status"] or "unknown"
        counts[status] = counts.get(status, 0) + 1

    return {
        "tasks": tasks,
        "counts": counts,
        "active": sum(counts.get(s, 0) for s in ("pending", "processing", "pause_requested")),
        "failed": counts.get("failed", 0),
        "paused": counts.get("paused", 0),
        "completed": counts.get("completed", 0),
    }
