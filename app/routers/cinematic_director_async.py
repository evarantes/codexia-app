from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import User
from app.routers.auth import get_current_admin_user
from app.routers.cinematic_campaign import (
    _rebalance_plan_to_budget,
    _settings,
    _usd_brl,
    _user_id,
)
from app.services.cinematic_director import CinematicDirector, CinematicDirectorError
from app.services.cinematic_director_job_store import DirectorJobStore


router = APIRouter(prefix="/cinematic", tags=["Codexia Cinematic Director Async"])
_store = DirectorJobStore()
_active_jobs: set[str] = set()
_active_lock = threading.RLock()


class AsyncDirectorRequest(BaseModel):
    request_id: str = Field(..., min_length=8, max_length=120)
    theme: str = Field(..., min_length=3, max_length=500)
    content_type: str = Field("story", pattern="^(story|devotional|short)$")
    duration_minutes: int = Field(10, ge=1, le=30)
    budget_brl: float = Field(90.0, ge=1, le=1000)


def _active_key(user_id: int, job_id: str) -> str:
    return f"{int(user_id or 0)}:{job_id}"


def _public_job(job: Dict[str, Any], *, reused: bool = False) -> Dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "message": job.get("message"),
        "result": job.get("result"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "reused": bool(reused),
    }


def _run_job(user_id: int, job_id: str, request_payload: Dict[str, Any]) -> None:
    key = _active_key(user_id, job_id)
    try:
        _store.update(
            user_id,
            job_id,
            status="running",
            stage="calling_claude",
            progress=15,
            message="Claude está criando roteiro, gancho, cenas e direção visual.",
        )
        db: Optional[Session] = None
        try:
            db = SessionLocal()
            settings = _settings(db, user_id)
            result = CinematicDirector(settings).build_plan(
                theme=str(request_payload.get("theme") or ""),
                content_type=str(request_payload.get("content_type") or "story"),
                duration_minutes=int(request_payload.get("duration_minutes") or 10),
                budget_brl=float(request_payload.get("budget_brl") or 90.0),
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

        _store.update(
            user_id,
            job_id,
            stage="optimizing_budget",
            progress=85,
            message="Direção recebida. Ajustando cenas A/B/C ao teto de custo.",
        )
        plan = _rebalance_plan_to_budget(
            result.plan,
            content_type=str(request_payload.get("content_type") or "story"),
            duration_minutes=int(request_payload.get("duration_minutes") or 10),
            budget_brl=float(request_payload.get("budget_brl") or 90.0),
        )
        usd_brl = _usd_brl()
        response = {
            "plan": plan,
            "director": {
                "provider": result.provider,
                "model": result.model,
                "usage": result.usage,
                "estimated_cost_usd": round(result.estimated_cost_usd, 4),
                "estimated_cost_brl": round(result.estimated_cost_usd * usd_brl, 2),
            },
        }
        _store.update(
            user_id,
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            message="Direção concluída e pronta para revisão.",
            result=response,
            error=None,
        )
    except CinematicDirectorError as exc:
        _store.update(
            user_id,
            job_id,
            status="failed",
            stage="failed",
            progress=100,
            message="Claude não conseguiu concluir a direção.",
            error=str(exc),
        )
    except Exception as exc:
        _store.update(
            user_id,
            job_id,
            status="failed",
            stage="failed",
            progress=100,
            message="A direção falhou no servidor.",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        with _active_lock:
            _active_jobs.discard(key)


def _launch_once(user_id: int, job_id: str, request_payload: Dict[str, Any]) -> bool:
    key = _active_key(user_id, job_id)
    with _active_lock:
        if key in _active_jobs:
            return False
        _active_jobs.add(key)
    thread = threading.Thread(
        target=_run_job,
        args=(user_id, job_id, dict(request_payload)),
        daemon=True,
        name=f"codexia-director-{job_id[:18]}",
    )
    thread.start()
    return True


@router.post("/director/jobs")
def create_director_job(
    body: AsyncDirectorRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    del db  # dependency keeps auth/database behavior aligned with the other cinematic routes
    uid = _user_id(current_user)
    try:
        job_id = _store.validate_job_id(body.request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    request_payload = {
        "theme": body.theme,
        "content_type": body.content_type,
        "duration_minutes": body.duration_minutes,
        "budget_brl": body.budget_brl,
    }
    try:
        job, created = _store.create_if_absent(uid, job_id, request_payload)
        if created:
            try:
                _store.cleanup(older_than_days=14)
            except Exception:
                pass
            _launch_once(uid, job_id, request_payload)
        return _public_job(job, reused=not created)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/director/jobs/{job_id}")
def get_director_job(
    job_id: str,
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    try:
        job = _store.read(uid, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Direção assíncrona não encontrada.")
    return _public_job(job)
