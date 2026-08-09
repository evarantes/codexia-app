"""Adaptador da fila agendada para o pipeline canônico de vídeo.

O scheduler não renderiza mais por uma segunda implementação. Ele apenas
normaliza ``ScheduledVideo`` para ``UnifiedVideoRequest`` e entrega o trabalho
ao mesmo executor usado por História/Devocional (Texto -> Vídeo).
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, Optional

from app.database import SessionLocal
from app.models import ScheduledVideo
from app.services.media_probe import probe_media_file
from app.services.task_manager import get_task
from app.services.unified_video_pipeline import (
    build_unified_video_request,
    unified_video_pipeline,
)


_PROCESSABLE_QUEUE_STATUSES = ("queued", "dispatching")


def _load_scheduled_processing_policy(video):
    from app.routers.youtube import _load_scheduled_video_payload, _scheduled_video_processing_policy

    payload = _load_scheduled_video_payload(video)
    policy = _scheduled_video_processing_policy(video, payload)
    return payload, policy


def _record_processing_refusal(video, payload, policy, trigger_mode: str):
    payload = dict(payload or {})
    reason = str(policy.get("reason") or "scheduled_video_source_not_allowed").strip()
    source = str(policy.get("source") or payload.get("source") or "legacy_schedule").strip()
    note = f"[AUTO_BLOCKED]: Processamento {trigger_mode} recusado para source={source}. Motivo: {reason}."
    payload.update(
        {
            "_processing_refused": True,
            "_processing_refused_reason": reason,
            "_processing_refused_source": source,
            "_processing_refused_trigger_mode": trigger_mode,
            "_processing_refused_at": datetime.datetime.now().isoformat(),
        }
    )
    video.script_data = json.dumps(payload, ensure_ascii=False, default=str)
    current_desc = (video.description or "").strip()
    if note not in current_desc:
        video.description = (current_desc + "\n\n" + note).strip() if current_desc else note


def _claim_scheduled_video_for_processing(db, video_id: int) -> bool:
    updated = (
        db.query(ScheduledVideo)
        .filter(
            ScheduledVideo.id == video_id,
            ScheduledVideo.status.in_(_PROCESSABLE_QUEUE_STATUSES),
        )
        .update(
            {
                ScheduledVideo.status: "processing",
                ScheduledVideo.updated_at: datetime.datetime.now(),
            },
            synchronize_session=False,
        )
    )
    if updated:
        db.commit()
        return True
    db.rollback()
    return False


def _task_video_url(task: Dict[str, Any]) -> Optional[str]:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    candidates = [
        result.get("video_url"),
        result.get("video_path"),
        result.get("file_path"),
        ((result.get("rendering") or {}).get("output_url") if isinstance(result.get("rendering"), dict) else None),
    ]
    for value in candidates:
        clean = str(value or "").strip()
        if clean:
            return clean
    return None


def sync_scheduled_video_from_task(db, video: ScheduledVideo) -> bool:
    """Espelha o estado real da VideoTask canônica no item agendado."""
    task_id = str(getattr(video, "task_id", None) or "").strip()
    if not task_id:
        return False
    task = get_task(task_id) or {}
    if not task:
        return False

    status = str(task.get("status") or "").strip().lower()
    try:
        video.progress = max(0, min(100, int(task.get("progress") or 0)))
    except Exception:
        video.progress = int(video.progress or 0)
    video.updated_at = datetime.datetime.now()

    if status in {"completed", "awaiting_review", "approved", "published"}:
        video_url = _task_video_url(task)
        if video_url:
            video.video_url = video_url
            if hasattr(video, "video_path"):
                video.video_path = video_url
        video.status = "published" if status == "published" else "completed"
        video.progress = 100
    elif status in {"failed", "cancelled"}:
        video.status = status
        if hasattr(video, "last_error"):
            video.last_error = str(task.get("message") or "Falha no pipeline canônico.")[:4000]
    elif status in {"pending", "queued", "processing"}:
        video.status = "processing"
    else:
        return False
    db.commit()
    return True


def _script_text_from_payload(payload: Dict[str, Any]) -> str:
    direct = str(payload.get("story_content") or payload.get("script_text") or "").strip()
    if direct:
        return direct
    scenes = payload.get("scenes")
    if not isinstance(scenes, list):
        return ""
    return "\n\n".join(
        str(scene.get("text") or "").strip()
        for scene in scenes
        if isinstance(scene, dict) and str(scene.get("text") or "").strip()
    )


def process_scheduled_video(video_id: int, trigger_mode: str = "auto"):
    """Enfileira um ScheduledVideo no pipeline História/Devocional."""
    db = SessionLocal()
    video: Optional[ScheduledVideo] = None
    try:
        video = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(video_id)).first()
        if not video:
            return

        payload, policy = _load_scheduled_processing_policy(video)
        if not policy.get("auto_process_eligible"):
            _record_processing_refusal(video, payload, policy, trigger_mode)
            db.commit()
            return

        if str(video.status or "").lower() in {"completed", "ready", "published"}:
            return
        if str(getattr(video, "task_id", None) or "").strip() and sync_scheduled_video_from_task(db, video):
            if str(video.status or "").lower() in {"processing", "completed", "published"}:
                return

        if video.video_url:
            try:
                from app.config import absolute_path_for_video

                candidate = absolute_path_for_video(video.video_url)
                probe = probe_media_file(candidate) if candidate and os.path.exists(candidate) else {}
                if bool(probe.get("ok")):
                    video.status = "completed"
                    video.progress = 100
                    db.commit()
                    return
            except Exception:
                pass

        if not _claim_scheduled_video_for_processing(db, int(video.id)):
            return
        video = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(video_id)).first()
        if not video:
            return

        raw = dict(payload or {})
        duration = raw.get("duration") or (1 if str(video.video_type or "").lower() == "short" else 3)
        raw.update(
            {
                "source_id": f"scheduled:{int(video.id)}",
                "idempotency_key": str(raw.get("idempotency_key") or f"scheduled-video:{int(video.id)}"),
                "topic": str(video.title or video.theme or "Vídeo agendado"),
                "story_content": _script_text_from_payload(raw),
                "duration": duration,
                "mode": "story" if _script_text_from_payload(raw) else "topic",
                "kind": "short" if str(video.video_type or "").lower() == "short" else str(raw.get("kind") or "custom"),
                "aspect_ratio": "9:16" if str(video.video_type or "").lower() == "short" else str(raw.get("aspect_ratio") or "16:9"),
                "voice_style": str(video.voice_style or raw.get("voice_style") or "human"),
                "voice_gender": str(video.voice_gender or raw.get("voice_gender") or "female"),
                "music_file_path": str(video.music_file_path or raw.get("music_file_path") or "") or None,
                "auto_upload": bool(video.auto_post),
                "review_required": not bool(video.auto_post),
                "user_id": getattr(video, "user_id", None),
                "scheduled_video_id": int(video.id),
            }
        )
        if isinstance(payload, dict) and isinstance(payload.get("scenes"), list):
            raw["seeded_script"] = dict(payload)

        request = build_unified_video_request(
            raw,
            source_module="scheduled",
            source_id=f"scheduled:{int(video.id)}",
            user_id=getattr(video, "user_id", None),
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
                "source_module": "scheduled",
                "scheduled_video_id": int(video.id),
                "pipeline": "unified_video_pipeline",
                "payload": raw,
            },
            user=None,
        )
        video.task_id = str(result.task_id or "") or None
        video.unified_video_id = result.unified_video_id
        if hasattr(video, "pipeline"):
            video.pipeline = "unified_video_pipeline"
        video.status = "completed" if result.reused_completed else "processing"
        video.progress = 100 if result.reused_completed else max(1, int(result.queue_position or 1))
        if result.video_url:
            video.video_url = result.video_url
        db.commit()
    except Exception as exc:
        db.rollback()
        if video is not None:
            try:
                video.status = "failed"
                video.progress = 0
                if hasattr(video, "last_error"):
                    video.last_error = f"{type(exc).__name__}: {str(exc)}"[:4000]
                db.commit()
            except Exception:
                db.rollback()
        raise
    finally:
        db.close()
