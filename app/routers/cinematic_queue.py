from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import UNIFIED_VIDEO_URL_PREFIX, absolute_path_for_video
from app.database import get_db
from app.models import UnifiedVideo, UnifiedVideoStatus, User, VideoTask
from app.routers.auth import get_current_admin_user
from app.services.cinematic_library_store import CinematicLibraryStore
from app.services.intelligent_cost_optimizer import minimum_visual_count_for_duration
from app.services.production_manifest import build_recovery_plan
from app.services.production_manifest_diagnostics import build_manifest_diagnostic
from app.services.task_manager import update_task
from app.services.unified_video_pipeline import unified_video_pipeline
from app.services.youtube_service import YouTubeService


router = APIRouter(prefix="/cinematic", tags=["Codexia Cinematic Queue"])
_library = CinematicLibraryStore()

_ACTIVE_STATUSES = {"pending", "processing", "pause_requested", "paused"}
_READY_STATUSES = {"completed", "awaiting_review", "ready", "awaiting_publish", "approved", "published"}
_FAILED_STATUSES = {"failed"}
_CANCELLED_STATUSES = {"cancelled", "canceled"}


class LibraryRegisterRequest(BaseModel):
    task_id: str = Field(..., min_length=3, max_length=191)
    title: Optional[str] = Field(None, max_length=180)
    project_slot: str = Field("story", pattern="^(story|devotional|short)$")
    duration_minutes: Optional[int] = Field(None, ge=1, le=180)


class ReviewDecisionRequest(BaseModel):
    notes: Optional[str] = Field(None, max_length=2000)


class PublishRequest(BaseModel):
    visibility: str = Field("unlisted", pattern="^(private|unlisted|public)$")


def _uid(current_user: Optional[User]) -> int:
    return int(getattr(current_user, "id", 0) or 0)


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


def _nested_dicts(value: Any, *, max_depth: int = 4) -> Iterable[Dict[str, Any]]:
    """Yield result containers without following arbitrary large scene arrays."""
    if not isinstance(value, dict):
        return
    queue: List[tuple[Dict[str, Any], int]] = [(value, 0)]
    seen: set[int] = set()
    nested_keys = (
        "payload",
        "result",
        "video_result",
        "render_report",
        "output",
        "final",
        "artifacts",
    )
    while queue:
        current, depth = queue.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        if depth >= max_depth:
            continue
        for key in nested_keys:
            child = current.get(key)
            if isinstance(child, dict):
                queue.append((child, depth + 1))


def _browser_video_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        return value
    if value.startswith(("/media/videos/", "/static/videos/")):
        return value
    if value.startswith("/data/media/videos/"):
        relative = value.split("/data/media/videos/", 1)[1].lstrip("/")
        return f"{UNIFIED_VIDEO_URL_PREFIX}/{relative}" if relative else ""
    static_marker = "/app/static/videos/"
    normalized = value.replace("\\", "/")
    if static_marker in normalized:
        relative = normalized.split(static_marker, 1)[1].lstrip("/")
        return f"/static/videos/{relative}" if relative else ""
    if normalized.startswith("app/static/videos/"):
        return "/static/videos/" + normalized.split("app/static/videos/", 1)[1]

    # Some older render reports persist only a filename/relative path. Expose it
    # only when the canonical resolver confirms that the MP4 actually exists.
    try:
        resolved = absolute_path_for_video(value)
        if resolved and os.path.isfile(resolved):
            return f"{UNIFIED_VIDEO_URL_PREFIX}/{os.path.basename(resolved)}"
    except Exception:
        pass
    return ""


def _video_url(result: Dict[str, Any]) -> str:
    for view in _nested_dicts(result):
        for key in ("video_url", "public_video_url", "file_url", "video_path", "file_path"):
            candidate = _browser_video_url(view.get(key))
            if candidate:
                return candidate
    return ""


def _title(
    row: VideoTask,
    result: Dict[str, Any],
    payload: Dict[str, Any],
    library_entry: Optional[Dict[str, Any]] = None,
) -> str:
    entry = library_entry if isinstance(library_entry, dict) else {}
    for value in (
        entry.get("title"),
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


def _bucket(status: str) -> str:
    value = str(status or "").strip().lower()
    if value in _ACTIVE_STATUSES:
        return "active"
    if value in _READY_STATUSES:
        return "ready"
    if value in _FAILED_STATUSES:
        return "failed"
    if value in _CANCELLED_STATUSES:
        return "cancelled"
    return "other"


def _seconds(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _artifact_checklist(
    row: VideoTask,
    result: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    duration_minutes: Optional[int],
    video_url: str,
) -> Dict[str, Any]:
    """Expose a concise, recovery-aware inventory for the V2 project modal."""
    task_id = str(getattr(row, "id", "") or "")
    persisted = result.get("artifact_checklist") if isinstance(result.get("artifact_checklist"), dict) else {}
    try:
        diagnostic = build_manifest_diagnostic(task_id)
    except Exception:
        diagnostic = {}
    try:
        recovery = build_recovery_plan(task_id)
    except Exception:
        recovery = {}

    target_sec = _seconds(persisted.get("target_duration_sec"))
    if target_sec <= 0 and duration_minutes:
        target_sec = float(duration_minutes) * 60.0

    persisted_images = persisted.get("images") if isinstance(persisted.get("images"), dict) else {}
    diagnosed_images = diagnostic.get("images") if isinstance(diagnostic.get("images"), dict) else {}
    image_actual = max(
        _integer(persisted_images.get("actual")),
        _integer(diagnosed_images.get("valid")),
        _integer(recovery.get("valid_image_count")),
    )
    image_expected = max(
        _integer(persisted_images.get("expected")),
        _integer(diagnosed_images.get("expected")),
        _integer(recovery.get("expected_image_count")),
    )
    if image_expected <= 0:
        requested = payload.get("image_count") or payload.get("expected_image_count")
        try:
            image_expected = max(0, int(requested or 0))
        except Exception:
            image_expected = 0
    images_ok = bool(image_expected > 0 and image_actual >= image_expected)

    persisted_audio = persisted.get("narration") if isinstance(persisted.get("narration"), dict) else {}
    audio_duration = max(
        _seconds(persisted_audio.get("duration_sec")),
        _seconds(recovery.get("audio_duration_sec")),
        _seconds((result.get("recovery_checkpoint") or {}).get("audio_duration_sec") if isinstance(result.get("recovery_checkpoint"), dict) else 0),
    )
    audio_found = bool(audio_duration > 0 or recovery.get("audio_found"))
    audio_reusable = bool(
        persisted_audio.get("preserved")
        or recovery.get("audio_reusable")
        or (diagnostic.get("audio") or {}).get("reusable")
    )
    audio_target = _seconds(persisted_audio.get("target_sec")) or target_sec
    audio_tolerance = max(5.0, audio_target * 0.05) if audio_target > 0 else 5.0
    audio_ok = bool(
        audio_found
        and audio_reusable
        and (
            audio_target <= 0
            or abs(audio_duration - audio_target) <= audio_tolerance
            or (0.90 * audio_target) <= audio_duration <= audio_target
        )
    )

    persisted_captions = persisted.get("captions") if isinstance(persisted.get("captions"), dict) else {}
    caption_checkpoint = result.get("caption_checkpoint") if isinstance(result.get("caption_checkpoint"), dict) else {}
    render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
    sync = render_report.get("sync_validation") if isinstance(render_report.get("sync_validation"), dict) else {}
    srt = render_report.get("srt") if isinstance(render_report.get("srt"), dict) else {}
    caption_duration = max(
        _seconds(persisted_captions.get("duration_sec")),
        _seconds(caption_checkpoint.get("duration_sec")),
        _seconds(sync.get("captions_duration_sec")),
        _seconds(sync.get("spoken_audio_end_sec")),
    )
    caption_entries = max(
        _integer(persisted_captions.get("entries")),
        _integer(caption_checkpoint.get("entries")),
        _integer(srt.get("entries")),
    )
    caption_source = str(
        persisted_captions.get("source")
        or caption_checkpoint.get("source")
        or sync.get("timeline_source")
        or srt.get("source")
        or ""
    ).strip()
    caption_target = _seconds(persisted_captions.get("target_sec")) or audio_duration
    caption_ok = bool(
        caption_entries > 0
        and caption_duration > 0
        and (
            caption_target <= 0
            or abs(caption_duration - caption_target) <= max(1.0, caption_target * 0.02)
        )
    )

    persisted_compatibility = (
        persisted.get("narration_caption_compatibility")
        if isinstance(persisted.get("narration_caption_compatibility"), dict)
        else {}
    )
    text_integrity = render_report.get("text_integrity") if isinstance(render_report.get("text_integrity"), dict) else {}
    utf8_audit = render_report.get("utf8_audit") if isinstance(render_report.get("utf8_audit"), dict) else {}
    trusted_timing_sources = {
        "official_audio_transcript",
        "approved_edge_tts_word_boundaries",
        "local_audio_activity_alignment",
    }
    timing_source = str(
        persisted_compatibility.get("timing_source")
        or caption_source
        or sync.get("caption_timeline_source")
        or sync.get("timeline_source")
        or ""
    ).strip()
    timing_source_verified = bool(
        persisted_compatibility.get("timing_source_verified")
        if "timing_source_verified" in persisted_compatibility
        else timing_source in trusted_timing_sources
    )
    text_matches_raw = persisted_compatibility.get("text_matches")
    if text_matches_raw is None:
        text_matches_raw = text_integrity.get("captions_match_narration_source")
    if text_matches_raw is None:
        text_matches_raw = utf8_audit.get("texts_identical_after_whitespace_normalization")
    text_matches = bool(text_matches_raw)
    sync_delta = _seconds(persisted_compatibility.get("duration_difference_sec"))
    if sync_delta <= 0 and audio_duration > 0 and caption_duration > 0:
        sync_delta = abs(audio_duration - caption_duration)
    sync_tolerance = (
        _seconds(persisted_compatibility.get("tolerance_sec"))
        or max(1.0, audio_duration * 0.02)
    )
    narration_caption_compatible = bool(
        audio_found
        and caption_entries > 0
        and text_matches
        and timing_source_verified
        and sync_delta <= sync_tolerance
    )
    task_message_lower = str(getattr(row, "message", "") or "").lower()
    quality_gate_blocked = bool(
        "image_count_minimum" in task_message_lower
        or (
            not video_url
            and image_actual > 0
            and image_expected > image_actual
        )
    )
    director_verdict = str(
        persisted_compatibility.get("director_verdict")
        or (
            "Narração e legenda compatíveis"
            if narration_caption_compatible
            else "Narração e legenda precisam de correção"
        )
    )

    status = str(getattr(row, "status", "") or "").strip().lower()
    render_ok = bool(video_url and status in _READY_STATUSES)
    persisted_script = persisted.get("script") if isinstance(persisted.get("script"), dict) else {}
    script_ok = bool(
        persisted_script.get("preserved")
        or diagnostic.get("script_preserved")
        or recovery.get("script_ok")
        or result.get("script")
    )
    items = [
        {
            "key": "script",
            "label": "Roteiro",
            "status": "ok" if script_ok else "missing",
            "summary": "Preservado" if script_ok else "Não encontrado",
            "preserved": script_ok,
        },
        {
            "key": "images",
            "label": "Imagens",
            "status": "ok" if images_ok else ("partial" if image_actual else "missing"),
            "summary": f"{image_actual}/{image_expected or '?'}",
            "actual": image_actual,
            "expected": image_expected or None,
            "preserved": image_actual > 0,
        },
        {
            "key": "narration",
            "label": "Narração",
            "status": "ok" if audio_ok else ("partial" if audio_found else "missing"),
            "summary": "Áudio preservado" if audio_reusable else ("Áudio encontrado" if audio_found else "Não encontrado"),
            "duration_sec": round(audio_duration, 3) if audio_duration > 0 else None,
            "target_sec": round(audio_target, 3) if audio_target > 0 else None,
            "preserved": audio_reusable,
        },
        {
            "key": "captions",
            "label": "Legendas",
            "status": "ok" if caption_ok else ("partial" if caption_entries else "missing"),
            "summary": f"{caption_entries} bloco(s)" if caption_entries else "Não preservadas",
            "duration_sec": round(caption_duration, 3) if caption_duration > 0 else None,
            "target_sec": round(caption_target, 3) if caption_target > 0 else None,
            "entries": caption_entries,
            "source": caption_source or None,
            "preserved": bool(persisted_captions.get("preserved") or caption_checkpoint),
        },
        {
            "key": "narration_caption_sync",
            "label": "Narração ↔ legenda",
            "status": "ok" if narration_caption_compatible else ("partial" if audio_found and caption_entries else "missing"),
            "summary": director_verdict,
            "duration_difference_sec": round(sync_delta, 3),
            "tolerance_sec": round(sync_tolerance, 3),
            "text_matches": text_matches,
            "timing_source_verified": timing_source_verified,
            "timing_source": timing_source or None,
            "director_validated": True,
        },
        {
            "key": "render",
            "label": "Vídeo final",
            "status": "ok" if render_ok else ("failed" if status == "failed" else "pending"),
            "summary": (
                "Pronto para revisão"
                if render_ok
                else (
                    "Quality Gate bloqueou antes da revisão; ativos anteriores preservados"
                    if quality_gate_blocked
                    else ("Render falhou; ativos anteriores preservados" if status == "failed" else "Pendente")
                )
            ),
            "preserved": render_ok,
        },
    ]
    reusable_assets = [item for item in items if item.get("key") in {"script", "images", "narration", "captions"}]
    reusable = sum(1 for item in reusable_assets if item.get("preserved"))
    return {
        "target_duration_sec": round(target_sec, 3) if target_sec > 0 else None,
        "items": items,
        "reusable_count": reusable,
        "reusable_total": len(reusable_assets),
        "director_validation": {
            "narration_caption_compatible": narration_caption_compatible,
            "verdict": director_verdict,
            "text_matches": text_matches,
            "timing_source_verified": timing_source_verified,
            "duration_difference_sec": round(sync_delta, 3),
            "tolerance_sec": round(sync_tolerance, 3),
        },
        "recovery_checkpoint": str(diagnostic.get("max_recoverable_checkpoint") or "starting"),
        "recovery_action": str(diagnostic.get("planned_action") or recovery.get("action") or "blocked"),
    }


def _task_to_public(
    row: VideoTask,
    library_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result = _result_obj(row)
    payload = _payload(result)
    entry = library_entry if isinstance(library_entry, dict) else {}
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
        or result.get("pipeline_stage")
        or ""
    ).strip()

    error = str(
        result.get("error")
        or result.get("last_error")
        or (message if status == "failed" else "")
        or ""
    ).strip()

    duration = entry.get("duration_minutes")
    if duration is None:
        duration = payload.get("duration") or payload.get("duration_minutes")
    try:
        duration = int(duration) if duration is not None else None
    except Exception:
        duration = None

    kind = str(
        entry.get("project_slot")
        or payload.get("kind")
        or result.get("kind")
        or ""
    ).strip().lower()
    url = _video_url(result)
    ready_for_watch = status in _READY_STATUSES and bool(url)
    youtube_url = ""
    for view in _nested_dicts(result):
        candidate = str(view.get("youtube_url") or "").strip()
        if candidate:
            youtube_url = candidate
            break

    checklist = _artifact_checklist(
        row,
        result,
        payload,
        duration_minutes=duration,
        video_url=url,
    )

    return {
        "id": str(getattr(row, "id", "") or ""),
        "title": _title(row, result, payload, entry),
        "status": status,
        "status_group": _bucket(status),
        "progress": progress,
        "stage": stage,
        "message": message,
        "error": error,
        "kind": kind,
        "duration_minutes": duration,
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "registered_at": entry.get("registered_at"),
        "origin": "cinematic_v2" if entry else "",
        "video_url": url or None,
        "can_watch": ready_for_watch,
        "can_view": True,
        "can_retry": status in {"failed", "paused"},
        "can_pause": status in {"pending", "processing"},
        "can_cancel": status in {"pending", "processing", "pause_requested", "paused"},
        "can_delete": status not in _ACTIVE_STATUSES,
        "can_approve": status == "awaiting_review",
        "can_reject": status == "awaiting_review",
        "can_publish": status == "approved",
        "youtube_url": youtube_url or None,
        "artifact_checklist": checklist,
    }


def _row_for_owned_task(db: Session, task_id: str, uid: int) -> VideoTask:
    row = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Projeto/tarefa não encontrado.")
    if uid > 0 and int(getattr(row, "user_id", 0) or 0) != uid:
        raise HTTPException(status_code=404, detail="Projeto/tarefa não encontrado.")
    return row


def _registered_owned_task(db: Session, task_id: str, uid: int) -> tuple[VideoTask, Dict[str, Any]]:
    entry = _library.get(uid, task_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Este projeto não pertence à biblioteca do Codexia V2.")
    return _row_for_owned_task(db, task_id, uid), entry


def _unified_for_task(db: Session, task_id: str) -> UnifiedVideo:
    unified = db.query(UnifiedVideo).filter(UnifiedVideo.task_id == str(task_id)).first()
    if not unified:
        raise HTTPException(status_code=409, detail="O registro unificado desta produção não foi encontrado.")
    return unified


def _review_record(*, decision: str, notes: str, user_id: int) -> Dict[str, Any]:
    return {
        "decision": decision,
        "notes": notes,
        "reviewed_by": int(user_id or 0) or None,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/library/register")
def register_v2_project(
    body: LibraryRegisterRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    """Register a canonical task as a project created explicitly in V2.

    This endpoint is deliberately separate from the legacy task table: only a
    successful V2 handoff calls it, so historic/legacy projects never appear in
    the new library merely because they share the same canonical executor.
    """
    uid = _uid(current_user)
    row = _row_for_owned_task(db, body.task_id, uid)
    result = _result_obj(row)
    payload = _payload(result)
    entry = _library.register(
        uid,
        str(row.id),
        title=body.title or _title(row, result, payload),
        project_slot=body.project_slot,
        duration_minutes=body.duration_minutes,
    )
    return {"registered": True, "project": _task_to_public(row, entry)}


@router.get("/queue")
def get_operational_queue(
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(8, ge=4, le=20),
    status: str = Query("all", pattern="^(all|active|ready|failed|cancelled)$"),
    q: str = Query("", max_length=120),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    """Return only projects explicitly registered by the Codexia V2 UI."""
    uid = _uid(current_user)
    entries = _library.list(uid)
    task_ids = [str(item.get("task_id") or "").strip() for item in entries]
    task_ids = [task_id for task_id in task_ids if task_id]

    if not task_ids:
        return {
            "tasks": [],
            "counts": {"all": 0, "active": 0, "ready": 0, "failed": 0, "cancelled": 0},
            "pagination": {"page": 1, "page_size": page_size, "total": 0, "pages": 0},
            "scope": "cinematic_v2_only",
        }

    query = db.query(VideoTask).filter(VideoTask.id.in_(task_ids))
    if uid > 0:
        query = query.filter(VideoTask.user_id == uid)
    rows = query.all()
    rows_by_id = {str(row.id): row for row in rows}
    entries_by_id = {str(item.get("task_id")): item for item in entries}

    tasks = [
        _task_to_public(rows_by_id[task_id], entries_by_id.get(task_id))
        for task_id in task_ids
        if task_id in rows_by_id
    ]
    tasks.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("registered_at") or ""),
        reverse=True,
    )

    counts = {
        "all": len(tasks),
        "active": sum(1 for item in tasks if item["status_group"] == "active"),
        "ready": sum(1 for item in tasks if item["status_group"] == "ready"),
        "failed": sum(1 for item in tasks if item["status_group"] == "failed"),
        "cancelled": sum(1 for item in tasks if item["status_group"] == "cancelled"),
    }

    search = str(q or "").strip().lower()
    if search:
        tasks = [
            item for item in tasks
            if search in str(item.get("title") or "").lower()
            or search in str(item.get("id") or "").lower()
        ]
    if status != "all":
        tasks = [item for item in tasks if item.get("status_group") == status]

    total = len(tasks)
    pages = (total + page_size - 1) // page_size if total else 0
    safe_page = min(page, max(1, pages)) if pages else 1
    start = (safe_page - 1) * page_size
    page_items = tasks[start:start + page_size]

    return {
        "tasks": page_items,
        "counts": counts,
        "pagination": {
            "page": safe_page,
            "page_size": page_size,
            "total": total,
            "pages": pages,
        },
        "scope": "cinematic_v2_only",
    }


@router.get("/queue/{task_id}")
def get_v2_project_details(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _uid(current_user)
    row, entry = _registered_owned_task(db, task_id, uid)
    return {"project": _task_to_public(row, entry)}


@router.post("/queue/{task_id}/approve")
def approve_v2_project(
    task_id: str,
    body: ReviewDecisionRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _uid(current_user)
    row, entry = _registered_owned_task(db, task_id, uid)
    status = str(getattr(row, "status", "") or "").strip().lower()
    if status != "awaiting_review":
        raise HTTPException(status_code=409, detail="Somente um projeto aguardando revisão pode ser aprovado.")

    validation = unified_video_pipeline().validate_before_awaiting_review(
        db,
        task_id,
        probe_local_paths=True,
        probe_http=False,
    )
    if not validation.ok:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "director_quality_validation_failed",
                "message": "O Claude Diretor bloqueou a aprovação porque o vídeo não cumpre o contrato de qualidade.",
                "first_failed": validation.first_failed,
                "checks": validation.checks,
                "details": validation.details,
            },
        )

    notes = str(body.notes or "").strip()
    review = _review_record(decision="approved", notes=notes, user_id=uid)
    unified = _unified_for_task(db, task_id)
    unified.status = UnifiedVideoStatus.APPROVED
    unified.approved_at = datetime.utcnow()
    unified.review_feedback_json = json.dumps(review, ensure_ascii=False)

    result = _result_obj(row)
    result["review"] = review
    row.status = "approved"
    row.progress = 100
    row.message = "Vídeo aprovado na revisão. Pronto para publicar no YouTube."
    row.result_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    update_task(task_id, status="approved", progress=100, message=row.message, result=result)
    return {"approved": True, "project": _task_to_public(row, entry)}


@router.post("/queue/{task_id}/reject")
def reject_v2_project(
    task_id: str,
    body: ReviewDecisionRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _uid(current_user)
    row, entry = _registered_owned_task(db, task_id, uid)
    status = str(getattr(row, "status", "") or "").strip().lower()
    if status != "awaiting_review":
        raise HTTPException(status_code=409, detail="Somente um projeto aguardando revisão pode ser reprovado.")
    notes = str(body.notes or "").strip()
    if not notes:
        raise HTTPException(status_code=422, detail="Descreva o que precisa ser corrigido antes de reprovar.")

    review = _review_record(decision="rejected", notes=notes, user_id=uid)
    unified = _unified_for_task(db, task_id)
    unified.status = UnifiedVideoStatus.FAILED
    unified.last_error = f"Reprovado na revisão: {notes}"[:1000]
    unified.review_feedback_json = json.dumps(review, ensure_ascii=False)

    result = _result_obj(row)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}

    # A reprovação do Diretor pode manter uma meta visual antiga no payload
    # (ex.: 8 imagens de um render de 5 min). Recalcule a meta a partir da
    # duração contratada antes de oferecer qualquer retry de economia.
    try:
        duration_minutes = int(
            getattr(unified, "duration_minutes", 0)
            or (entry or {}).get("duration_minutes")
            or payload.get("duration")
            or 0
        )
    except Exception:
        duration_minutes = 0
    duration_minutes = max(0, duration_minutes)
    quality_floor = minimum_visual_count_for_duration(duration_minutes)

    recovery_budget = (
        dict(payload.get("recovery_image_budget"))
        if isinstance(payload.get("recovery_image_budget"), dict)
        else {}
    )
    try:
        previous_visual_target = max(
            int(payload.get("expected_image_count") or 0),
            int(recovery_budget.get("expected_image_count") or 0),
        )
    except Exception:
        previous_visual_target = 0
    strict_visual_target = max(previous_visual_target, quality_floor)

    if strict_visual_target > 0:
        recovery_budget["expected_image_count"] = strict_visual_target
        recovery_budget["quality_recalculated"] = True
        payload["recovery_image_budget"] = recovery_budget
        payload["expected_image_count"] = strict_visual_target
        payload["strict_visual_target_count"] = strict_visual_target
    if duration_minutes > 0:
        payload["duration"] = duration_minutes

    payload.update({
        "review_feedback": notes,
        "force_regenerate": True,
        "force_reuse_assets": False,
        "force_render_only": False,
        "editorial_reviewed": False,
        "editorial_review_ready": False,
        "director_quality_required": True,
        "strict_visual_quality_required": True,
    })
    result["payload"] = payload
    result["review"] = review
    row.status = "failed"
    row.progress = 100
    row.message = f"Reprovado na revisão: {notes}"[:1000]
    row.result_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    update_task(task_id, status="failed", progress=100, message=row.message, result=result)
    return {"rejected": True, "project": _task_to_public(row, entry)}


@router.post("/queue/{task_id}/publish")
def publish_v2_project(
    task_id: str,
    body: PublishRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _uid(current_user)
    row, entry = _registered_owned_task(db, task_id, uid)
    status = str(getattr(row, "status", "") or "").strip().lower()
    if status != "approved":
        raise HTTPException(status_code=409, detail="Aprove o vídeo antes de publicá-lo no YouTube.")

    unified = _unified_for_task(db, task_id)
    try:
        script = json.loads(unified.script_json or "{}") if unified.script_json else {}
    except Exception:
        script = {}
    if not isinstance(script, dict):
        script = {}
    result = _result_obj(row)
    title = str(script.get("title") or _title(row, result, _payload(result), entry)).strip()[:100]
    description = str(script.get("description") or "Vídeo produzido por Codexia.").strip()
    tags = script.get("tags") if isinstance(script.get("tags"), list) else []

    youtube = YouTubeService()

    def upload(video_path: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        response = youtube.upload_video(
            video_path,
            title=str(metadata.get("title") or title),
            description=str(metadata.get("description") or description),
            tags=metadata.get("tags") if isinstance(metadata.get("tags"), list) else tags,
            thumbnail_path=str(unified.cover_path) if unified.cover_path else None,
            privacy_status=str(metadata.get("visibility") or body.visibility),
        )
        return response if isinstance(response, dict) else {"error": "Resposta inválida do YouTube."}

    published = unified_video_pipeline().publish_if_ready(
        db,
        task_id,
        upload_callable=upload,
        upload_metadata={
            "title": title,
            "description": description,
            "tags": tags,
            "visibility": body.visibility,
        },
        visibility_override=body.visibility,
    )
    if not published.get("ok"):
        raise HTTPException(status_code=502, detail=published.get("error") or "Não foi possível publicar no YouTube.")

    result.update({
        "youtube_video_id": published.get("youtube_video_id"),
        "youtube_url": published.get("youtube_url"),
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    row.status = "published"
    row.progress = 100
    row.message = "Vídeo publicado no YouTube com sucesso."
    row.result_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    update_task(task_id, status="published", progress=100, message=row.message, result=result)
    return {
        "published": True,
        "youtube_video_id": published.get("youtube_video_id"),
        "youtube_url": published.get("youtube_url"),
        "project": _task_to_public(row, entry),
    }


@router.delete("/queue/{task_id}")
def delete_v2_project_from_library(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    """Soft-delete a V2 library item while preserving canonical audit records."""
    uid = _uid(current_user)
    row, _entry = _registered_owned_task(db, task_id, uid)
    status = str(getattr(row, "status", "") or "").strip().lower()
    if status in _ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Este projeto ainda está ativo. Cancele ou aguarde a produção antes de excluí-lo da biblioteca.",
        )
    if not _library.soft_delete(uid, task_id):
        raise HTTPException(status_code=404, detail="Projeto não encontrado na biblioteca V2.")
    return {
        "deleted": True,
        "task_id": str(task_id),
        "message": "Projeto removido da biblioteca V2. O registro técnico do pipeline foi preservado para auditoria.",
    }
