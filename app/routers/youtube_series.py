from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional

from app.database import get_db
from app.models import SeriesEpisode, SeriesPlan, User
from app.routers.auth import get_current_user
from app.services.youtube_series_service import youtube_series_service


router = APIRouter(prefix="/youtube/series", tags=["YouTube Series"])


class SeriesCreateRequest(BaseModel):
    name: str
    main_theme: str
    start_date: str
    end_date: str
    total_episodes: Optional[int] = 1
    objective: Optional[str] = None
    target_audience: Optional[str] = None
    content_type: Optional[str] = "reflection"
    publication_time: Optional[str] = "19:00"
    timezone: Optional[str] = "UTC"
    production_lead_days: Optional[int] = 1
    production_time: Optional[str] = "06:00"
    duration_minutes: Optional[int] = 10
    visibility: Optional[str] = "unlisted"
    tone: Optional[str] = None
    narration_style: Optional[str] = "human"
    continuity_level: Optional[str] = "medium"
    hook_intensity: Optional[str] = "medium"
    use_biblical_references: Optional[bool] = True
    cta_subscribe: Optional[bool] = True
    cta_next_episode: Optional[bool] = True
    auto_approval: Optional[bool] = False
    status: Optional[str] = "draft"


class SeriesStatusUpdateRequest(BaseModel):
    status: str


class EpisodeUpdateRequest(BaseModel):
    planned_title: Optional[str] = None
    narrated_title: Optional[str] = None
    summary: Optional[str] = None
    publication_datetime: Optional[str] = None
    duration_minutes: Optional[int] = None


class EpisodeReviewRequest(BaseModel):
    reason_categories: List[str]
    feedback: str


@router.get("")
def list_series(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return youtube_series_service.list_series(db, user=current_user)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.get("/review-queue")
def review_queue(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        items: List[Dict[str, Any]] = []
        listing = youtube_series_service.list_series(db, user=current_user)
        for series_item in (listing.get("items") if isinstance(listing, dict) else []) or []:
            series_id = int((series_item or {}).get("id") or 0)
            if not series_id:
                continue
            detail = youtube_series_service.get_series_detail(db, user=current_user, series_id=series_id)
            for ep in (detail.get("episodes") if isinstance(detail, dict) else []) or []:
                status = str((ep or {}).get("status") or "").strip().lower()
                if status in {"awaiting_review", "publication_blocked"}:
                    items.append(ep)
        items.sort(key=lambda row: (str(row.get("publication_datetime") or ""), int(row.get("episode_number") or 0)))
        return {"items": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.get("/{series_id}")
def get_series_detail(
    series_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return youtube_series_service.get_series_detail(db, user=current_user, series_id=int(series_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.post("")
def create_series(
    payload: SeriesCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return youtube_series_service.create_series(db, user=current_user, payload=payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.put("/{series_id}/status")
def update_series_status(
    series_id: int,
    payload: SeriesStatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return youtube_series_service.update_series_status(db, user=current_user, series_id=int(series_id), status=str(payload.status))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.put("/episodes/{episode_id}")
def update_episode(
    episode_id: int,
    payload: EpisodeUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return youtube_series_service.update_episode_plan(db, user=current_user, episode_id=int(episode_id), payload=payload.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.post("/sync")
def sync_series(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        result = youtube_series_service.sync_series_scheduler(db)
        if isinstance(result, dict):
            return result
        return {"queued": 0}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.post("/episodes/{episode_id}/approve")
def approve_episode(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return youtube_series_service.approve_episode(db, user=current_user, episode_id=int(episode_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.post("/episodes/{episode_id}/correction-plan")
def preview_correction_plan(
    episode_id: int,
    payload: EpisodeReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        episode = (
            db.query(SeriesEpisode)
            .join(SeriesPlan, SeriesPlan.id == SeriesEpisode.series_id)
            .filter(SeriesEpisode.id == int(episode_id), SeriesPlan.user_id == int(current_user.id))
            .first()
        )
        if not episode:
            raise ValueError("Episódio não encontrado.")
        return youtube_series_service.build_correction_plan(
            episode,
            reasons=list(payload.reason_categories or []),
            feedback=str(payload.feedback or ""),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@router.post("/episodes/{episode_id}/reject")
def reject_episode(
    episode_id: int,
    payload: EpisodeReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return youtube_series_service.reject_episode(
            db,
            user=current_user,
            episode_id=int(episode_id),
            reasons=list(payload.reason_categories or []),
            feedback=str(payload.feedback or ""),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])
