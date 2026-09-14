from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import absolute_path_for_video
from app.database import get_db
from app.models import User, VideoTask
from app.routers.auth import get_current_admin_user
from app.services.cinematic_compositor import CinematicCompositor, CinematicCompositorError


router = APIRouter(prefix="/cinematic", tags=["Codexia Cinematic Compose"])


class MotionClip(BaseModel):
    scene_index: int = Field(..., ge=1, le=500)
    output_url: Optional[str] = None
    clip_path: Optional[str] = None
    start_sec: Optional[float] = Field(None, ge=0)
    end_sec: Optional[float] = Field(None, ge=0)
    motion_seconds: Optional[float] = Field(None, ge=0.2, le=30)


class ComposeRequest(BaseModel):
    base_task_id: str = Field(..., min_length=3, max_length=128)
    motion_clips: List[MotionClip] = Field(..., min_items=1, max_items=60)
    output_filename: Optional[str] = Field(None, max_length=180)


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _decode_result(row: VideoTask) -> Dict[str, Any]:
    try:
        data = json.loads(str(row.result_json or "{}"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _result_views(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    views: List[Dict[str, Any]] = [data]
    for key in ("payload", "result", "video_result"):
        value = data.get(key)
        if isinstance(value, dict):
            views.append(value)
    return views


def _find_render_report(data: Dict[str, Any]) -> Dict[str, Any]:
    for view in _result_views(data):
        report = view.get("render_report")
        if isinstance(report, dict):
            return report
    return {}


def _find_base_video(data: Dict[str, Any], render_report: Dict[str, Any]) -> str:
    candidates: List[str] = []
    for view in _result_views(data):
        for key in ("file_path", "video_path", "video_url"):
            raw = str(view.get(key) or "").strip()
            if raw:
                candidates.append(raw)
    for key in ("file_path", "video_path", "video_url"):
        raw = str(render_report.get(key) or "").strip()
        if raw:
            candidates.append(raw)
    for raw in candidates:
        if raw.lower().startswith(("http://", "https://")):
            continue
        if os.path.isfile(raw):
            return os.path.abspath(raw)
        try:
            resolved = absolute_path_for_video(raw)
            if resolved and os.path.isfile(resolved):
                return os.path.abspath(resolved)
        except Exception:
            pass
    return ""


@router.get("/compose/base/{task_id}")
def inspect_base_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    row = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tarefa base não encontrada.")
    uid = int(getattr(current_user, "id", 0) or 0)
    if uid and row.user_id not in (None, uid):
        raise HTTPException(status_code=404, detail="Tarefa base não encontrada.")
    data = _decode_result(row)
    report = _find_render_report(data)
    base = _find_base_video(data, report)
    windows = CinematicCompositor.scene_windows_from_render_report(report)
    return {
        "task_id": row.id,
        "status": row.status,
        "base_video_ready": bool(base),
        "base_video_path": base if base else None,
        "scene_windows": windows,
        "srt_available": bool(str(((report.get("srt") or {}).get("path") or "")).strip()),
    }


@router.post("/compose")
def compose_cinematic_video(
    body: ComposeRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    row = db.query(VideoTask).filter(VideoTask.id == str(body.base_task_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tarefa base não encontrada.")
    uid = int(getattr(current_user, "id", 0) or 0)
    if uid and row.user_id not in (None, uid):
        raise HTTPException(status_code=404, detail="Tarefa base não encontrada.")
    if str(row.status or "").lower() not in {"completed", "ready", "awaiting_publish"}:
        raise HTTPException(status_code=409, detail=f"A tarefa base ainda não está concluída (status: {row.status}).")

    data = _decode_result(row)
    report = _find_render_report(data)
    base_path = _find_base_video(data, report)
    if not base_path:
        raise HTTPException(status_code=409, detail="O MP4 da tarefa base não foi encontrado no volume persistente.")

    motion_payload: List[Dict[str, Any]] = []
    for clip in body.motion_clips:
        item = clip.model_dump()
        if not str(item.get("output_url") or item.get("clip_path") or "").strip():
            raise HTTPException(status_code=422, detail=f"Cena {item.get('scene_index')}: informe output_url ou clip_path.")
        motion_payload.append(item)

    try:
        result = CinematicCompositor().compose(
            base_video_path=base_path,
            motion_clips=motion_payload,
            render_report=report,
            output_filename=body.output_filename,
        )
        result["base_task_id"] = row.id
        result["hybrid_mode"] = "canonical_base_plus_generative_motion"
        return result
    except CinematicCompositorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
