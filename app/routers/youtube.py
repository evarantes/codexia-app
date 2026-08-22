import os
import glob
import shutil
import json
import uuid
import threading
import time
import sys
import multiprocessing
import math
import hashlib
import re
from datetime import datetime, timedelta, timezone
try:
    from filelock import FileLock, Timeout
except Exception:
    # Fallback para ambientes sem dependência instalada.
    # Mantém o app inicializando e evita quebra total do deploy.
    class Timeout(Exception):
        pass

    class FileLock:  # type: ignore
        def __init__(self, *_args, **_kwargs):
            self._locked = False

        def acquire(self, *_args, **_kwargs):
            self._locked = True
            return True

        def release(self):
            self._locked = False
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
try:
    from rq import Worker
    RQ_AVAILABLE = True
except Exception:
    # No Windows, RQ pode falhar devido ao fork()
    RQ_AVAILABLE = False
    Worker = None
from app.services.youtube_service import YouTubeService
from app.services.ai_generator import AIContentGenerator
from app.services.story_review_editor import generate_review_ready_story_text
from app.services.ai_router import AICapability, AIOperationBlocked
from app.services.cinematic_quality_service import CinematicQualityService
from app.services.financial_guardian import youtube_auto_financial_adapter
from app.services.financial_guardian.youtube_observability import youtube_financial_guardian_observability_service
from app.services.financial_guardian_service import financial_guardian_service
from app.services.global_settings_service import get_latest_settings, serialize_official_factory_settings
from app.services.media_probe import media_durations_match, probe_media_file
from app.services.task_manager import (
    create_task,
    update_task,
    merge_task_result,
    get_task,
    get_task_by_idempotency_key,
    is_task_cancel_requested,
    is_task_pause_requested,
    request_cancel_task,
    request_pause_task,
    mark_task_paused,
    enqueue_paused_task_for_resume,
    reset_task_for_retry,
    claim_video_task,
    acquire_distributed_lock,
    release_distributed_lock,
    acquire_task_execution_lease,
    heartbeat_task_execution_lease,
    get_task_execution_lease,
    release_task_execution_lease,
    finalize_task_once,
)
from app.services.youtube_auto_responder import auto_thank_comments
from app.database import get_db, SessionLocal
from app.services.video_factory import VideoFactory
from app.modules.bible_video_factory.editorial_intelligence import (
    EditorialIntelligenceService,
    normalize_editorial_intelligence_settings,
)
from app.models import ScheduledVideo, ChannelReport, Settings, ContentPlan, Video, Job, Asset, Scene, CommunityComment, CommunityPost, StoryDraft, SystemNotification, ChannelInsight, VideoTask, User, SeriesEpisode, SeriesPlan, UnifiedVideo, UnifiedVideoStatus
from app.modules.ai_factory.models import AIImage
from app.redis_client import conn, queue as rq_queue
from app.routers.auth import get_current_admin_user, SECRET_KEY as _AUTH_SECRET_KEY, ALGORITHM as _AUTH_ALGORITHM

FACTORY_LOCK_KEY = "codexia:video_factory:single_worker_lock"

#region debug-point youtube-finalize-stuck
_DEBUG_ENV_PATH = os.path.join(".dbg", "youtube-finalize-stuck.env")

def _dbg_event(hypothesis_id: str, msg: str, data: Optional[Dict[str, Any]] = None):
    try:
        import json as _json
        import urllib.request as _urlreq

        url = "http://127.0.0.1:7777/event"
        session_id = "youtube-finalize-stuck"
        if os.path.exists(_DEBUG_ENV_PATH):
            try:
                with open(_DEBUG_ENV_PATH, "r", encoding="utf-8") as f:
                    for line in f.read().splitlines():
                        if line.startswith("DEBUG_SERVER_URL="):
                            url = line.split("=", 1)[1].strip() or url
                        elif line.startswith("DEBUG_SESSION_ID="):
                            session_id = line.split("=", 1)[1].strip() or session_id
            except Exception:
                pass

        run_id = str(os.getenv("DEBUG_RUN_ID") or "pre").strip() or "pre"
        payload = {
            "sessionId": session_id,
            "runId": run_id,
            "hypothesisId": str(hypothesis_id or "").strip() or "NA",
            "location": "app/routers/youtube.py",
            "msg": str(msg or ""),
            "data": data or {},
        }
        req = _urlreq.Request(
            url,
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        _urlreq.urlopen(req, timeout=0.25).read()
    except Exception:
        pass
#endregion
_CANCEL_ALL_KEY = "codexia:video_cancel_all"
_CANCEL_ALL_TTL_SECONDS = 90
_CANCEL_ALL_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_SCHEDULED_VIDEO_ACTIVE_STATUSES = (
    "pending",
    "queued",
    "dispatching",
    "processing",
    "completed",
    "ready",
    "awaiting_publish",
)
# Lock file para quando Redis não está disponível (garante 1 job por vez)
_lock_dir = "/data" if os.path.isdir("/data") else os.path.expanduser("~")
_FACTORY_LOCK_PATH = os.path.join(_lock_dir, ".codexia_factory.lock")

def _cancel_all_active() -> bool:
    if not conn:
        return False
    try:
        v = conn.get(_CANCEL_ALL_KEY)
        return bool(v)
    except Exception:
        return False


def _activate_cancel_all_barrier() -> Optional[str]:
    """Bloqueia novos trabalhos somente enquanto o encerramento está em curso.

    O TTL continua sendo uma proteção para queda abrupta do processo, mas o
    token precisa ser removido assim que a rota terminar. Sem isso, uma série
    criada depois do encerramento herda o sinal global e é cancelada sem ação
    do usuário.
    """
    if not conn:
        return None
    token = uuid.uuid4().hex
    try:
        acquired = conn.set(
            _CANCEL_ALL_KEY,
            token,
            ex=_CANCEL_ALL_TTL_SECONDS,
            nx=True,
        )
        return token if acquired else None
    except Exception:
        return None


def _release_cancel_all_barrier(token: Optional[str]) -> None:
    if not conn or not token:
        return
    try:
        # Compare-and-delete atômico: um encerramento antigo nunca remove a
        # barreira pertencente a uma requisição mais nova.
        conn.eval(_CANCEL_ALL_RELEASE_SCRIPT, 1, _CANCEL_ALL_KEY, str(token))
    except Exception:
        # Falha segura: o TTL limpa a chave. É preferível bloquear por alguns
        # segundos a apagar, por engano, a barreira de outro encerramento.
        pass

def _rq_video_timeout_seconds() -> int:
    raw = (os.getenv("RQ_VIDEO_TIMEOUT") or os.getenv("RQ_DEFAULT_TIMEOUT") or "").strip()
    try:
        v = int(raw) if raw else 14400
    except Exception:
        v = 14400
    return max(600, v)

def _rq_workers_online() -> bool:
    """Retorna True quando o RQ registra worker ativo para a fila de vídeo.

    Não use um corte fixo curto em ``last_heartbeat``. Enquanto o worker está
    ocioso, o RQ pode manter o registro válido por vários minutos entre ciclos
    de manutenção. O próprio registro/TTL do RQ é a fonte de verdade para
    monitoramento; em produção continuamos fail-closed se nenhum worker estiver
    registrado.
    """
    if not conn or not RQ_AVAILABLE or Worker is None:
        return False
    try:
        if rq_queue is not None:
            try:
                return Worker.count(queue=rq_queue) > 0
            except TypeError:
                pass
            except Exception:
                pass

        try:
            return Worker.count(connection=conn) > 0
        except TypeError:
            try:
                return Worker.count(conn) > 0
            except Exception:
                pass
        except Exception:
            pass

        # Compatibilidade defensiva com versões/classes customizadas de RQ.
        try:
            workers = list(Worker.all(connection=conn))
        except TypeError:
            workers = list(Worker.all(conn))
        return bool(workers)
    except Exception:
        return False

def _is_video_factory_busy() -> bool:
    if conn:
        try:
            lock = conn.lock(FACTORY_LOCK_KEY, timeout=5, blocking_timeout=0)
            acquired = lock.acquire(blocking=False)
            if acquired:
                try:
                    lock.release()
                except Exception:
                    pass
                return False
            return True
        except Exception:
            pass
    try:
        lock = FileLock(_FACTORY_LOCK_PATH, timeout=0)
        lock.acquire()
        try:
            lock.release()
        except Exception:
            pass
        return False
    except Timeout:
        return True
    except Exception:
        return False

def _video_task_result_payload(result_obj: Any) -> Dict[str, Any]:
    if not isinstance(result_obj, dict):
        return {}
    payload = result_obj.get("payload")
    return payload if isinstance(payload, dict) else {}

def _video_task_result_obj(row: VideoTask) -> Optional[Dict[str, Any]]:
    if not row or not row.result_json:
        return None
    try:
        data = json.loads(row.result_json)
    except Exception:
        return None
    return data if isinstance(data, dict) else None

def _video_task_title_from_row(row: VideoTask) -> str:
    result_obj = _video_task_result_obj(row) or {}
    payload = _video_task_result_payload(result_obj)

    if _is_story_video_generation_task(result_obj):
        return _story_video_task_title_from_payload(payload)

    title_hint = str(result_obj.get("title_hint") or "").strip()
    if title_hint:
        return title_hint[:120]

    kind = str(result_obj.get("kind") or "").strip().lower()
    if kind == "music_clip":
        return "Clipe musical"
    if kind == "music_shorts":
        return "Shorts musicais"
    if kind == "music_distribution":
        return "Distribuição musical"

    msg = str(getattr(row, "message", "") or "").strip()
    if msg:
        return msg[:120]
    return "Outra atividade do servidor"

def _video_task_source_label(row: VideoTask) -> str:
    result_obj = _video_task_result_obj(row) or {}
    if _is_story_video_generation_task(result_obj):
        return "Fila desta geração"
    kind = str(result_obj.get("kind") or "").strip().lower()
    if kind == "music_clip":
        return "Clipe musical"
    if kind == "music_shorts":
        return "Shorts musicais"
    if kind == "music_distribution":
        return "Distribuição musical"
    return "Outra atividade do servidor"

def _is_story_video_generation_task(result_obj: Any) -> bool:
    if not isinstance(result_obj, dict):
        return False
    kind = str(result_obj.get("kind") or "").strip().lower()
    if kind == "youtube_story_video":
        return True
    payload = _video_task_result_payload(result_obj)
    mode = str(payload.get("mode") or "").strip().lower()
    if mode in {"story", "topic"}:
        return True
    return False

def _story_video_task_title_from_payload(payload: Dict[str, Any]) -> str:
    title = str(payload.get("override_title") or payload.get("topic") or "").strip()
    if title:
        return title[:120]
    story_content = str(payload.get("story_content") or "").strip()
    if story_content:
        first_line = next((ln.strip() for ln in story_content.splitlines() if ln.strip()), "")
        if first_line:
            return first_line[:120]
    mode = str(payload.get("mode") or "").strip().lower()
    return "Vídeo narrado" if mode in {"story", "topic"} else "Tarefa de vídeo"


def _normalize_hash_text(value: Any, *, lower: bool = False) -> str:
    txt = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt.lower() if lower else txt


def _normalize_hash_list(values: Any, *, lower: bool = False) -> List[str]:
    items: List[str] = []
    if isinstance(values, list):
        for item in values:
            txt = _normalize_hash_text(item, lower=lower)
            if txt:
                items.append(txt)
    return sorted(items)


def _build_video_generation_canonical_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = _normalize_hash_text(payload.get("mode") or "topic", lower=True) or "topic"
    kind = _normalize_hash_text(payload.get("kind") or "story", lower=True) or "story"
    image_mode = _normalize_hash_text(payload.get("image_mode") or "", lower=True)
    voice_style = _normalize_hash_text(payload.get("voice_style") or "")
    voice_gender = _normalize_hash_text(payload.get("voice_gender") or "", lower=True)
    aspect_ratio = _normalize_hash_text(payload.get("aspect_ratio") or "16:9", lower=True) or "16:9"
    override_tags = _normalize_hash_list(payload.get("override_tags") or [], lower=True)
    selected_images = _normalize_hash_list(payload.get("selected_images") or [])
    custom_image_paths = _normalize_hash_list(payload.get("custom_image_paths") or [])
    canonical = {
        "mode": mode,
        "kind": kind,
        "topic": _normalize_hash_text(payload.get("topic") or ""),
        "story_content": _normalize_hash_text(payload.get("story_content") or ""),
        "duration": max(1, min(60, int(payload.get("duration") or 5))),
        "aspect_ratio": aspect_ratio,
        "auto_upload": bool(payload.get("auto_upload")),
        "voice_style": voice_style,
        "voice_gender": voice_gender,
        "image_mode": image_mode,
        "thumbnail_path": _normalize_hash_text(payload.get("thumbnail_path") or ""),
        "override_title": _normalize_hash_text(payload.get("override_title") or ""),
        "override_description": _normalize_hash_text(payload.get("override_description") or ""),
        "override_tags": override_tags,
        "selected_images": selected_images,
        "custom_image_paths": custom_image_paths,
    }
    return canonical


def _build_video_generation_identity(payload: Dict[str, Any]) -> Dict[str, Any]:
    canonical = _build_video_generation_canonical_payload(payload)
    canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    return {
        "canonical_payload": canonical,
        "canonical_json": canonical_json,
        "request_hash": request_hash,
        "idempotency_key": f"ytv1:{request_hash}",
    }


def _video_task_dedupe_window_seconds() -> int:
    raw = (os.getenv("YOUTUBE_VIDEO_DEDUPE_WINDOW_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 6 * 60 * 60
    except Exception:
        value = 6 * 60 * 60
    return max(60, min(7 * 24 * 60 * 60, value))


def _content_stable_str(value: Any, limit_chars: int = 2000) -> str:
    raw = str(value or "").replace("\r", "\n")
    try:
        import re as _re
        import unicodedata as _ud
        raw = _ud.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii", errors="ignore")
        raw = raw.lower()
        raw = _re.sub(r"\s+", " ", raw)
        raw = _re.sub(r"[^a-z0-9 ]", " ", raw)
        raw = _re.sub(r"\s+", " ", raw).strip()
    except Exception:
        try:
            raw = (raw or "").lower().strip()
        except Exception:
            raw = ""
    return (raw[: max(50, int(limit_chars or 2000))]) if raw else ""


def _payload_content_hash(payload: Dict[str, Any]) -> str:
    try:
        title = str(
            payload.get("override_title")
            or payload.get("title")
            or payload.get("title_hint")
            or payload.get("topic")
            or ""
        ).strip()
        text = str(
            payload.get("story_content")
            or payload.get("text")
            or payload.get("script_text")
            or payload.get("topic")
            or ""
        ).strip()
        minutes = int(payload.get("duration") or payload.get("minutes") or 0)
        kind = str(payload.get("kind") or payload.get("mode") or "").strip().lower()
        if not title and not text:
            return ""
        norm_title = _content_stable_str(title, limit_chars=500)
        norm_text = _content_stable_str(text, limit_chars=2000)
        joined = f"T:{norm_title}|X:{norm_text}|M:{int(minutes or 0)}|K:{kind}"
        try:
            import hashlib as _hash
            return f"c1:{_hash.sha256(joined.encode('utf-8', errors='ignore')).hexdigest()[:20]}"
        except Exception:
            return f"c1:{joined[:80]}"
    except Exception:
        return ""


def _window_hours_content_reuse() -> int:
    raw = (os.getenv("YOUTUBE_CONTENT_REUSE_WINDOW_HOURS") or "").strip()
    try:
        value = int(raw) if raw else 48
    except Exception:
        value = 48
    return max(1, min(30 * 24, value))


def _find_reusable_completed_task_by_content(
    db: Session,
    payload: Dict[str, Any],
    excluded_task_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    content_hash = _payload_content_hash(payload)
    if not content_hash:
        return None
    try:
        window_hours = _window_hours_content_reuse()
    except Exception:
        window_hours = 48
    threshold = datetime.utcnow() - timedelta(hours=window_hours)
    excluded_stripped = str(excluded_task_id or "").strip()
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.status == "completed")
        .filter(VideoTask.created_at >= threshold)
        .order_by(VideoTask.created_at.desc(), VideoTask.id.desc())
        .limit(500)
        .all()
    )
    for row in rows:
        if excluded_stripped and str(row.id) == excluded_stripped:
            continue
        try:
            result_obj = _video_task_result_obj(row) or {}
            stored_hash = str(result_obj.get("content_hash") or "").strip()
            row_payload = _video_task_result_payload(result_obj)
            computed = _payload_content_hash(row_payload) if row_payload else ""
            if not stored_hash and not computed:
                continue
            matches = bool(
                (stored_hash and stored_hash == content_hash)
                or (computed and computed == content_hash)
            )
            if not matches:
                continue
            video_url = str(result_obj.get("video_url") or row_payload.get("video_url") or "").strip()
            file_path = str(result_obj.get("file_path") or row_payload.get("file_path") or "").strip()
            abs_file = ""
            try:
                if file_path and not file_path.startswith("http"):
                    if not os.path.isabs(file_path):
                        try:
                            from app.config import absolute_path_for_video as _abs
                            abs_file = str(_abs(os.path.basename(file_path))) or ""
                        except Exception:
                            abs_file = ""
                    else:
                        abs_file = file_path
            except Exception:
                abs_file = ""
            if not (abs_file and os.path.isfile(abs_file)):
                continue
            media_probe = probe_media_file(abs_file)
            if not media_probe.get("ok") or not media_durations_match(media_probe):
                continue
            return {
                "task_id": str(row.id),
                "content_hash": content_hash,
                "video_url": video_url,
                "file_path": file_path,
                "title": str(result_obj.get("title") or row_payload.get("title") or result_obj.get("title_hint") or ""),
                "result_obj": result_obj,
            }
        except Exception:
            continue
    return None


def _load_story_video_task_rows(
    db: Session,
    limit: int = 50,
    *,
    include_paused: bool = False,
) -> List[VideoTask]:
    statuses = ["pending", "processing", "pause_requested"]
    if include_paused:
        statuses.append("paused")
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.status.in_(statuses))
        .order_by(VideoTask.created_at.asc(), VideoTask.id.asc())
        .limit(max(1, min(200, int(limit or 50))))
        .all()
    )
    filtered: List[VideoTask] = []
    for row in rows:
        result_obj = None
        if row.result_json:
            try:
                result_obj = json.loads(row.result_json)
            except Exception:
                result_obj = None
        if _is_story_video_generation_task(result_obj):
            filtered.append(row)
    status_order = {"processing": 0, "pause_requested": 0, "pending": 1, "paused": 2}
    filtered.sort(key=lambda r: (status_order.get(str(r.status or "").lower(), 3), r.created_at or datetime.utcnow(), str(r.id)))
    return filtered


def _load_latest_recoverable_story_video_task(db: Session) -> Optional[VideoTask]:
    """Retorna somente a falha recente mais nova que ainda possui payload de retry."""
    try:
        raw_days = int((os.getenv("VIDEO_TASK_RECOVERY_DAYS") or "").strip() or "7")
    except Exception:
        raw_days = 7
    cutoff = datetime.utcnow() - timedelta(days=max(1, min(30, raw_days)))
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.status == "failed")
        .order_by(VideoTask.updated_at.desc().nullslast(), VideoTask.created_at.desc().nullslast())
        .limit(100)
        .all()
    )
    for row in rows:
        ref_dt = _task_row_reference_dt(row)
        if ref_dt and ref_dt < cutoff:
            continue
        result_obj = _video_task_result_obj(row)
        if not _is_story_video_generation_task(result_obj):
            continue
        if not _video_task_result_payload(result_obj):
            continue
        return row
    return None


def _story_video_stale_minutes() -> int:
    try:
        raw = int((os.getenv("VIDEO_TASK_STALE_MINUTES") or "").strip() or "1440")
    except Exception:
        raw = 1440
    return max(30, min(7 * 24 * 60, raw))


def _story_video_pending_expiration_minutes() -> int:
    try:
        raw = int((os.getenv("VIDEO_TASK_PENDING_EXPIRATION_MINUTES") or "").strip() or "720")
    except Exception:
        raw = 720
    return max(_story_video_stale_minutes(), min(14 * 24 * 60, raw))


def _task_row_reference_dt(row: VideoTask) -> Optional[datetime]:
    dt = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
    if not dt:
        return None
    try:
        if getattr(dt, "tzinfo", None) is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        pass
    return dt


def _task_payload_timestamp(value: Optional[str]) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _task_executor_is_alive(task_id: str) -> bool:
    try:
        lease = get_task_execution_lease(str(task_id)) or {}
        lease_expires = _task_payload_timestamp(lease.get("lease_expires_at") or lease.get("expires_at"))
        return bool(lease_expires and lease_expires > datetime.utcnow())
    except Exception:
        return False


def _cleanup_story_video_task_queue(db: Session, rows: Optional[List[VideoTask]] = None) -> Dict[str, Any]:
    rows = rows or _load_story_video_task_rows(db, limit=100)
    now = datetime.utcnow()
    stale_minutes = _story_video_stale_minutes()
    pending_expiration_minutes = _story_video_pending_expiration_minutes()
    cleaned: List[Dict[str, Any]] = []
    changed = False
    for row in rows:
        status = str(row.status or "").lower()
        ref_dt = _task_row_reference_dt(row)
        if not ref_dt:
            continue
        age_minutes = max(0.0, (now - ref_dt).total_seconds() / 60.0)
        if status == "processing" and age_minutes >= stale_minutes:
            recovered = False
            try:
                _res = _video_task_result_obj(row) or {}
                _cand = [
                    str(_res.get("file_path") or "").strip(),
                    str(_res.get("video_path") or "").strip(),
                    str(_res.get("video_url") or "").strip(),
                ]
                _abs_cand = ""
                try:
                    from app.config import absolute_path_for_video as _abs_f
                    for _c in _cand:
                        if not _c or _c.startswith("http"):
                            continue
                        _try = str(_abs_f(os.path.basename(_c))) if not os.path.isabs(_c) else _c
                        if _try and os.path.exists(_try):
                            _abs_cand = _try
                            break
                except Exception:
                    _abs_cand = ""
                if _abs_cand and os.path.exists(_abs_cand):
                    try:
                        _probe = probe_media_file(_abs_cand)
                        _sz = int(_probe.get("file_size_bytes") or 0)
                        _video_stream = bool(_probe.get("video_stream"))
                        _audio_stream = bool(_probe.get("audio_stream"))
                        _vdur = float(_probe.get("video_duration") or 0.0)
                        _adur = float(_probe.get("audio_duration") or 0.0)
                        _ok = bool(_probe.get("ok")) and media_durations_match(_probe)
                        if _ok:
                            new_result = dict(_res)
                            try:
                                new_result["final_validation"] = {
                                    "ok": True,
                                    "recovered": True,
                                    "checks": {
                                        "file_exists": True,
                                        "size_gt_100kb": bool(_sz > 100 * 1024),
                                        "video_stream": bool(_video_stream),
                                        "audio_stream": bool(_audio_stream),
                                        "duration_valid": _vdur > 0.5,
                                        "audio_not_trimmed": media_durations_match(_probe),
                                        "http_media_ready": True,
                                    },
                                    "size_bytes": _sz,
                                    "video_duration_sec": round(_vdur, 3),
                                    "audio_duration_sec": round(_adur, 3),
                                    "probe_available": bool(_probe.get("probe_available")),
                                }
                            except Exception:
                                pass
                            try:
                                import json as _json
                                row.result_json = _json.dumps(new_result, ensure_ascii=False)
                            except Exception:
                                pass
                            row.status = "completed"
                            row.progress = 100
                            row.message = (
                                f"Recuperação automática: arquivo MP4 pronto encontrado em disco "
                                f"({_sz // 1024} KB, {round(_vdur, 1)}s). Tarefa concluída — NOVA GERAÇÃO NÃO foi liberada."
                            )
                            cleaned.append({
                                "task_id": str(row.id),
                                "status_before": "processing",
                                "status_after": "completed",
                                "age_minutes": round(age_minutes, 2),
                                "recovered": True,
                            })
                            recovered = True
                            changed = True
                    except Exception:
                        recovered = False
            except Exception:
                recovered = False
            if not recovered:
                message = (
                    f"Falha automática: tarefa travada sem atualização há mais de "
                    f"{stale_minutes} min. Nova geração liberada."
                )
                row.status = "failed"
                row.message = message
                cleaned.append({
                    "task_id": str(row.id),
                    "status_before": "processing",
                    "status_after": "failed",
                    "age_minutes": round(age_minutes, 2),
                })
                changed = True
        elif status == "pending" and age_minutes >= pending_expiration_minutes:
            row.status = "cancelled"
            row.message = (
                f"Cancelado automaticamente: tarefa antiga removida da fila após "
                f"{pending_expiration_minutes} min sem execução."
            )
            cleaned.append({
                "task_id": str(row.id),
                "status_before": "pending",
                "status_after": "cancelled",
                "age_minutes": round(age_minutes, 2),
            })
            changed = True
    if changed:
        db.commit()
    return {
        "changed": changed,
        "cleaned": cleaned,
        "stale_minutes": stale_minutes,
        "pending_expiration_minutes": pending_expiration_minutes,
    }

def _story_video_task_item_from_row(row: VideoTask, position: int) -> Dict[str, Any]:
    result_obj = _video_task_result_obj(row) or {}
    payload = _video_task_result_payload(result_obj)
    status = str(row.status or "").lower()
    is_current = status in {"processing", "pause_requested"}
    task_snapshot = {
        "task_id": str(row.id),
        "status": status,
        "progress": int(row.progress or 0),
        "message": row.message,
        "created_at": (row.created_at.isoformat() if getattr(row, "created_at", None) else None),
        "updated_at": (row.updated_at.isoformat() if getattr(row, "updated_at", None) else None),
        "result": result_obj,
    }
    runtime = _runtime_view_for_task(task_snapshot)
    started_at = _task_payload_timestamp(result_obj.get("executor_started_at")) or getattr(row, "created_at", None)
    elapsed_seconds = None
    if started_at:
        try:
            if getattr(started_at, "tzinfo", None) is not None:
                started_at = started_at.astimezone(timezone.utc).replace(tzinfo=None)
            elapsed_seconds = max(0, int((datetime.utcnow() - started_at).total_seconds()))
        except Exception:
            elapsed_seconds = None
    return {
        "task_id": row.id,
        "status": row.status,
        "progress": int(row.progress or 0),
        "message": row.message,
        "created_at": (row.created_at.isoformat() if getattr(row, "created_at", None) else None),
        "updated_at": (row.updated_at.isoformat() if getattr(row, "updated_at", None) else None),
        "position": int(position),
        "is_current": is_current,
        "title": _story_video_task_title_from_payload(payload),
        "duration": payload.get("duration"),
        "mode": payload.get("mode"),
        "kind": result_obj.get("kind"),
        "source_type": "video_task",
        "source_label": "Fila desta geração",
        "queue_label": (
            "Pausa solicitada" if status == "pause_requested"
            else ("Em execução" if is_current else ("Pausada" if status == "paused" else "Na fila"))
        ),
        "can_open": True,
        "can_cancel": True,
        "can_pause": status in {"pending", "processing"},
        "can_resume": bool(status == "paused"),
        "cancel_kind": "task",
        "pause_kind": "task",
        "elapsed_seconds": elapsed_seconds,
        "runtime": runtime,
        "stage": runtime.get("stage") or result_obj.get("pipeline_stage"),
        "last_signal_seconds": runtime.get("last_signal_seconds"),
    }

def _active_video_task_blocker_item(db: Session, excluded_task_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    excluded = {str(v) for v in (excluded_task_ids or set()) if str(v).strip()}
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.status.in_(["pending", "processing", "pause_requested"]))
        .order_by(VideoTask.updated_at.desc().nullslast(), VideoTask.created_at.desc().nullslast())
        .limit(50)
        .all()
    )
    for row in rows:
        if str(row.id) in excluded:
            continue
        if str(row.status or "").lower() not in {"processing", "pause_requested"}:
            continue
        item = _story_video_task_item_from_row(row, 1)
        item["title"] = _video_task_title_from_row(row)
        item["source_label"] = _video_task_source_label(row)
        item["queue_label"] = "Ocupando o servidor"
        item["can_open"] = False
        return item
    return None

def _active_production_video_blocker_item(db: Session) -> Optional[Dict[str, Any]]:
    job = (
        db.query(Job)
        .join(Video, Video.id == Job.video_id)
        .filter(Job.status == "processing")
        .order_by(Job.updated_at.desc().nullslast(), Job.created_at.desc().nullslast(), Job.id.desc())
        .first()
    )
    if not job or not getattr(job, "video", None):
        return None

    video = job.video
    normalized_status = _normalize_video_status(video.status)
    fallback_progress = _progress_from_video_status(normalized_status)
    try:
        job_progress = int(job.progress or 0)
    except Exception:
        job_progress = 0

    progress = job_progress if job_progress > 0 else max(job_progress, fallback_progress)
    status_message = _last_log_line(job.logs)
    if not status_message:
        step = (job.step or "").strip().lower() or "produção"
        status_message = f"Etapa atual: {step}."

    duration = None
    try:
        if getattr(video, "duration_sec", None):
            duration = max(1, int(math.ceil(float(video.duration_sec) / 60.0)))
    except Exception:
        duration = None

    elapsed_seconds = None
    job_started_at = getattr(job, "created_at", None)
    if job_started_at:
        try:
            if getattr(job_started_at, "tzinfo", None) is not None:
                job_started_at = job_started_at.astimezone(timezone.utc).replace(tzinfo=None)
            elapsed_seconds = max(0, int((datetime.utcnow() - job_started_at).total_seconds()))
        except Exception:
            elapsed_seconds = None

    pause_requested = normalized_status == "PAUSED"
    cancel_requested = normalized_status == "CANCELLED"

    return {
        "task_id": None,
        "status": normalized_status or "PROCESSING",
        "progress": max(0, min(99, int(progress or 0))),
        "message": status_message,
        "created_at": (job.created_at.isoformat() if getattr(job, "created_at", None) else None),
        "updated_at": (job.updated_at.isoformat() if getattr(job, "updated_at", None) else None),
        "position": 1,
        "is_current": True,
        "title": (video.title or f"Vídeo #{video.id}")[:120],
        "duration": duration,
        "mode": "production_queue",
        "kind": "production_video",
        "source_type": "production_video",
        "source_label": "Fila principal de produção",
        "queue_label": (
            "Cancelamento solicitado" if cancel_requested
            else ("Pausa solicitada" if pause_requested else "Ocupando o servidor")
        ),
        "can_open": False,
        "can_cancel": not cancel_requested,
        "can_pause": not pause_requested and not cancel_requested,
        "can_resume": False,
        "cancel_kind": "production_video",
        "pause_kind": "production_video",
        "production_video_id": int(video.id),
        "elapsed_seconds": elapsed_seconds,
        "stage": (job.step or "production").strip().lower(),
    }


def _production_video_queue_item(db: Session, video: Video, position: int) -> Optional[Dict[str, Any]]:
    """Serializa um item da fila principal para o painel unificado."""
    jobs = (
        db.query(Job)
        .filter(Job.video_id == video.id)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(20)
        .all()
    )
    processing_job = next((job for job in jobs if str(job.status or "").lower() == "processing"), None)
    pending_job = next((job for job in reversed(jobs) if str(job.status or "").lower() == "pending"), None)
    paused_job = next((job for job in jobs if str(job.status or "").lower() == "paused"), None)
    active_job = processing_job or pending_job or paused_job or (jobs[0] if jobs else None)
    video_status = _normalize_video_status(video.status)

    if video_status == "PAUSED" or paused_job:
        status = "paused"
        queue_label = "Pausada"
        is_current = False
    elif processing_job:
        status = "processing"
        queue_label = "Ocupando o servidor"
        is_current = True
    elif pending_job or video_status == "QUEUED":
        status = "pending"
        queue_label = "Na fila"
        is_current = False
    elif video_status in {"PROCESSING", "SCRIPT", "TTS", "VISUALS", "RENDER"}:
        status = "processing"
        queue_label = "Ocupando o servidor"
        is_current = True
    else:
        return None

    try:
        job_progress = int(getattr(active_job, "progress", 0) or 0)
    except Exception:
        job_progress = 0
    progress = max(job_progress, _progress_from_video_status(video_status))
    if status == "paused" and job_progress:
        progress = job_progress
    message = _last_log_line(getattr(active_job, "logs", "") if active_job else "")
    if not message:
        if status == "processing":
            message = f"Processando etapa: {getattr(active_job, 'step', None) or 'produção'}..."
        elif status == "paused":
            message = "Produção pausada pelo usuário; aguardando retomada manual."
        else:
            message = "Aguardando vez na fila de produção."

    duration = None
    try:
        if getattr(video, "duration_sec", None):
            duration = max(1, int(math.ceil(float(video.duration_sec) / 60.0)))
    except Exception:
        duration = None

    created_at = getattr(active_job, "created_at", None) or getattr(video, "created_at", None)
    elapsed_seconds = None
    if is_current and created_at:
        try:
            current_start = created_at
            if getattr(current_start, "tzinfo", None) is not None:
                current_start = current_start.astimezone(timezone.utc).replace(tzinfo=None)
            elapsed_seconds = max(0, int((datetime.utcnow() - current_start).total_seconds()))
        except Exception:
            elapsed_seconds = None

    return {
        "task_id": None,
        "status": status,
        "progress": max(0, min(100, int(progress or 0))),
        "message": message,
        "created_at": (created_at.isoformat() if created_at else None),
        "updated_at": (
            active_job.updated_at.isoformat()
            if active_job and getattr(active_job, "updated_at", None)
            else None
        ),
        "position": int(position),
        "is_current": is_current,
        "title": (video.title or f"Vídeo #{video.id}")[:120],
        "duration": duration,
        "mode": "production_queue",
        "kind": "production_video",
        "source_type": "production_video",
        "source_label": "Fila principal de produção",
        "queue_label": queue_label,
        "can_open": False,
        "can_cancel": True,
        "can_pause": status in {"pending", "processing"},
        "can_resume": status == "paused",
        "cancel_kind": "production_video",
        "pause_kind": "production_video",
        "production_video_id": int(video.id),
        "elapsed_seconds": elapsed_seconds,
        "stage": (getattr(active_job, "step", None) or ("paused" if status == "paused" else "queued")),
    }


def _load_production_video_queue_items(
    db: Session,
    *,
    excluded_task_ids: Optional[set] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Carrega trabalhos ativos, aguardando e pausados da fila principal."""
    excluded = {str(value) for value in (excluded_task_ids or set()) if str(value).strip()}
    videos = (
        db.query(Video)
        .filter(func.upper(func.trim(Video.status)).in_([
            "QUEUED",
            "PROCESSING",
            "SCRIPT",
            "TTS",
            "VISUALS",
            "RENDER",
            "PAUSED",
        ]))
        .order_by(Video.created_at.asc(), Video.id.asc())
        .limit(max(1, min(200, int(limit or 100))))
        .all()
    )
    items: List[Dict[str, Any]] = []
    for video in videos:
        if getattr(video, "task_id", None) and str(video.task_id) in excluded:
            continue
        if getattr(video, "task_id", None):
            task = get_task(str(video.task_id)) or {}
            task_status = str(task.get("status") or "").strip().lower()
            if task_status in {
                "completed",
                "awaiting_review",
                "approved",
                "published",
                "failed",
                "cancelled",
            }:
                continue
        item = _production_video_queue_item(db, video, len(items) + 1)
        if item:
            items.append(item)
    return items

def _load_factory_blocker_item(db: Session, excluded_task_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    item = _active_video_task_blocker_item(db, excluded_task_ids=excluded_task_ids)
    if item:
        return item
    return _active_production_video_blocker_item(db)

def _is_valid_seed_script(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    scenes = value.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return False
    for scene in scenes[:3]:
        if isinstance(scene, dict) and str(scene.get("text") or "").strip():
            return True
        if isinstance(scene, str) and scene.strip():
            return True
    return False

def _file_ok(path_value: Any, *, min_bytes: int = 1000) -> bool:
    try:
        p = str(path_value or "").strip()
        if not p:
            return False
        if not os.path.exists(p):
            return False
        return os.path.getsize(p) >= int(min_bytes or 1)
    except Exception:
        return False

def _selected_images_ok(urls: List[str], *, min_bytes: int = 1000) -> bool:
    if not urls:
        return False
    try:
        from app.config import absolute_path_for_static
    except Exception:
        absolute_path_for_static = None
    checked = 0
    for url in urls:
        if not url:
            continue
        checked += 1
        if checked > 6:
            break
        try:
            if absolute_path_for_static:
                p = absolute_path_for_static(url)
            else:
                p = ""
        except Exception:
            p = ""
        if not (p and os.path.exists(p) and os.path.getsize(p) >= int(min_bytes or 1)):
            return False
    return True

def _maybe_enable_render_only_flags(payload: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    payload.setdefault("force_reuse_assets", True)
    if bool(payload.get("force_render_only")):
        return payload
    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
        if not row or not getattr(row, "result_json", None):
            return payload
        try:
            result_obj = json.loads(getattr(row, "result_json", "") or "{}")
        except Exception:
            result_obj = {}
        if not isinstance(result_obj, dict):
            return payload
        seed_script = result_obj.get("script") if isinstance(result_obj.get("script"), dict) else None
        seed_render_report = result_obj.get("render_report") if isinstance(result_obj.get("render_report"), dict) else {}
        audio_path = ""
        try:
            audio_path = str(((seed_render_report.get("audio_generation") or {}).get("output_path") or "")).strip()
        except Exception:
            audio_path = ""
        selected_images: List[str] = []
        if seed_script and isinstance(seed_script.get("selected_images"), list):
            selected_images = [
                str(x).strip()
                for x in seed_script.get("selected_images")
                if isinstance(x, str) and str(x).strip()
            ]
        script_ok = _is_valid_seed_script(seed_script)
        images_ok = _selected_images_ok(selected_images)
        audio_ok = _file_ok(audio_path)
        if script_ok and images_ok and audio_ok:
            payload["force_render_only"] = True
    finally:
        db.close()
    return payload


def _dispatch_task_result(task_id: str, payload: Dict[str, Any], executor: str, **extra: Any) -> Dict[str, Any]:
    """Registra o executor sem apagar roteiro, imagens, áudio ou relatórios anteriores."""
    current = get_task(task_id) or {}
    current_result = current.get("result") if isinstance(current, dict) else None
    merged = dict(current_result) if isinstance(current_result, dict) else {}
    merged.update({
        "payload": dict(payload or {}),
        "executor": str(executor or "thread"),
        "kind": "youtube_story_video",
    })
    if extra:
        merged.update(extra)
    return merged


def _is_youtube_series_payload(payload: Dict[str, Any], task_id: Optional[str] = None) -> bool:
    raw = payload if isinstance(payload, dict) else {}
    if str(raw.get("source_module") or "").strip().lower() == "youtube_series":
        return True
    if isinstance(raw.get("series_context"), dict):
        return True
    if not task_id:
        return False
    try:
        task = get_task(str(task_id)) or {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        nested_payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        return bool(
            isinstance(result.get("series_context"), dict)
            or isinstance(nested_payload.get("series_context"), dict)
            or str(nested_payload.get("source_module") or "").strip().lower() == "youtube_series"
        )
    except Exception:
        return False


def _video_payload_duration_minutes(payload: Dict[str, Any]) -> int:
    raw = payload if isinstance(payload, dict) else {}
    candidates = [
        raw.get("duration"),
        raw.get("duration_minutes"),
        raw.get("target_duration_min"),
        raw.get("duration_min"),
    ]
    seeded = raw.get("seeded_script") if isinstance(raw.get("seeded_script"), dict) else {}
    candidates.extend([
        seeded.get("target_duration_min"),
        seeded.get("duration_minutes"),
    ])
    for candidate in candidates:
        try:
            value = int(float(candidate))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 5


def _requires_isolated_video_process(payload: Dict[str, Any], task_id: Optional[str] = None) -> bool:
    """Mantém renderizações pesadas fora do processo web principal."""
    if _is_youtube_series_payload(payload, task_id):
        return True
    try:
        threshold = int((os.getenv("VIDEO_ISOLATED_PROCESS_MINUTES") or "5").strip() or "5")
    except Exception:
        threshold = 5
    threshold = max(2, min(30, threshold))
    return _video_payload_duration_minutes(payload) >= threshold


def _series_resource_preflight(
    payload: Dict[str, Any],
    task_id: str,
    *,
    persist_block: bool = True,
) -> Optional[Dict[str, Any]]:
    if not _is_youtube_series_payload(payload, task_id):
        return None
    raw = payload if isinstance(payload, dict) else {}
    try:
        duration_minutes = max(1, int(raw.get("duration") or raw.get("duration_minutes") or 10))
    except Exception:
        duration_minutes = 10
    from app.services.video_resource_guard import (
        evaluate_series_video_resources,
        resource_guard_message,
    )

    report = evaluate_series_video_resources(duration_minutes)
    report = {
        **report,
        "message": resource_guard_message(report),
        "checked_at": datetime.utcnow().isoformat(),
    }
    if persist_block and not bool(report.get("allowed")):
        update_task(
            task_id,
            status="failed",
            progress=0,
            message=str(report.get("message") or "Produção bloqueada pela proteção de recursos."),
            result=_dispatch_task_result(
                task_id,
                raw,
                "resource_guard",
                resource_guard=report,
                retryable=True,
            ),
        )
    return report


def _runtime_heartbeat_seconds() -> int:
    try:
        raw = int((os.getenv("VIDEO_RUNTIME_HEARTBEAT_SECONDS") or "").strip() or "15")
    except Exception:
        raw = 15
    return max(5, min(60, raw))


def _runtime_interruption_seconds() -> int:
    try:
        raw = int((os.getenv("VIDEO_RUNTIME_INTERRUPTION_SECONDS") or "").strip() or "300")
    except Exception:
        raw = 300
    return max(120, min(30 * 60, raw))


def _runtime_parse_dt(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if getattr(parsed, "tzinfo", None) is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def _runtime_view_for_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Traduz heartbeat e recursos em uma explicação curta para a interface."""
    task_obj = task if isinstance(task, dict) else {}
    status = str(task_obj.get("status") or "").strip().lower()
    result = task_obj.get("result") if isinstance(task_obj.get("result"), dict) else {}
    telemetry = result.get("runtime_telemetry") if isinstance(result.get("runtime_telemetry"), dict) else {}
    resource_health = telemetry.get("resource_health") if isinstance(telemetry.get("resource_health"), dict) else {}
    snapshot = resource_health.get("snapshot") if isinstance(resource_health.get("snapshot"), dict) else {}
    now = datetime.utcnow()
    interval = _runtime_heartbeat_seconds()
    interruption_threshold = max(interval * 6, _runtime_interruption_seconds())

    heartbeat_at = _runtime_parse_dt(telemetry.get("heartbeat_at"))
    lease_heartbeat = _runtime_parse_dt(task_obj.get("executor_heartbeat_at"))
    if not lease_heartbeat:
        try:
            lease = get_task_execution_lease(str(task_obj.get("task_id") or "")) or {}
            lease_heartbeat = _runtime_parse_dt(lease.get("heartbeat_at"))
        except Exception:
            lease_heartbeat = None
    if lease_heartbeat and (not heartbeat_at or lease_heartbeat > heartbeat_at):
        heartbeat_at = lease_heartbeat
    updated_at = _runtime_parse_dt(task_obj.get("updated_at") or task_obj.get("created_at"))
    stage_changed_at = _runtime_parse_dt(telemetry.get("stage_changed_at")) or updated_at

    heartbeat_age = max(0, int((now - heartbeat_at).total_seconds())) if heartbeat_at else None
    update_age = max(0, int((now - updated_at).total_seconds())) if updated_at else None
    stage_age = max(0, int((now - stage_changed_at).total_seconds())) if stage_changed_at else None
    level = str(resource_health.get("level") or "unknown").lower()

    if status == "paused":
        state = "paused"
        label = "Produção pausada"
        detail = "O servidor foi liberado e os ativos concluídos continuam preservados para retomada."
    elif status == "pause_requested":
        state = "pausing"
        label = "Pausa solicitada"
        detail = "A etapa atual está sendo concluída com segurança antes de liberar o servidor."
    elif status not in {"pending", "processing"}:
        state = "finished"
        label = "Processo finalizado"
        detail = "A tarefa não está mais em execução."
    elif heartbeat_age is not None and heartbeat_age <= interval * 3:
        if level == "critical":
            state = "resource_pressure"
            label = "Processando com pressão crítica"
            detail = "O processo está ativo, mas o servidor está com poucos recursos."
        elif level == "warning":
            state = "resource_warning"
            label = "Processando com recursos reduzidos"
            detail = "O processo está ativo e continua sendo acompanhado."
        else:
            state = "working"
            label = "Processo ativo"
            detail = "O servidor confirmou que a produção continua trabalhando."
    elif heartbeat_age is not None and heartbeat_age <= interruption_threshold:
        state = "delayed"
        label = "Sinal do processo atrasado"
        detail = "A produção pode estar aguardando uma operação externa demorada; o sistema ainda está dentro do limite de interrupção."
    elif heartbeat_age is not None:
        state = "possibly_interrupted"
        label = "Produção possivelmente interrompida"
        detail = "O executor ultrapassou o limite sem sinais. Verifique timeout do provedor, reinício do contêiner ou encerramento do processo."
    elif update_age is not None and update_age <= 60:
        state = "starting"
        label = "Iniciando monitoramento"
        detail = "A tarefa foi atualizada recentemente e aguarda o primeiro sinal detalhado."
    else:
        state = "unmonitored"
        label = "Sem telemetria detalhada"
        detail = "Esta execução começou sem o monitor contínuo; use Diagnosticar para conferir os recursos."

    reasons = [str(item) for item in (resource_health.get("reasons") or []) if str(item or "").strip()]
    return {
        "state": state,
        "label": label,
        "detail": detail,
        "heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        "last_signal_seconds": heartbeat_age,
        "stage_unchanged_seconds": stage_age,
        "stage": telemetry.get("stage") or result.get("pipeline_stage"),
        "resource_level": level,
        "resource_reasons": reasons,
        "resources": snapshot,
        "monitor_interval_seconds": interval,
        "interruption_threshold_seconds": interruption_threshold,
    }


def _start_video_runtime_monitor(task_id: str, executor_id: str):
    """Mantém heartbeat e telemetria durante roteiro, imagens, áudio e render."""
    stop_event = threading.Event()
    interval = _runtime_heartbeat_seconds()

    def _monitor():
        sequence = 0
        baseline_oom_kills: Optional[int] = None
        consecutive_inactive_reads = 0
        monitor_errors = 0
        last_monitor_error = None
        while not stop_event.is_set():
            try:
                # Renova primeiro o lease: uma leitura/mescla de telemetria não pode
                # fazer uma execução saudável parecer morta para o painel.
                heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
                task = get_task(task_id) or {}
                if str(task.get("status") or "").lower() not in {"pending", "processing"}:
                    consecutive_inactive_reads += 1
                    if consecutive_inactive_reads >= 2:
                        break
                    if stop_event.wait(interval):
                        break
                    continue
                consecutive_inactive_reads = 0
                from app.services.video_resource_guard import (
                    capture_resource_snapshot,
                    evaluate_runtime_resource_health,
                )

                snapshot = capture_resource_snapshot()
                events = snapshot.get("cgroup_memory_events") if isinstance(snapshot.get("cgroup_memory_events"), dict) else {}
                current_oom_kills = int(events.get("oom_kill") or 0)
                if baseline_oom_kills is None:
                    baseline_oom_kills = current_oom_kills
                oom_kill_delta = max(0, current_oom_kills - int(baseline_oom_kills or 0))
                resource_health = evaluate_runtime_resource_health(snapshot)
                if oom_kill_delta > 0:
                    resource_health = dict(resource_health)
                    resource_health["level"] = "critical"
                    resource_health["summary"] = "O sistema registrou encerramento por falta de memória durante esta execução."
                    resource_health["reasons"] = [
                        f"O kernel registrou {oom_kill_delta} encerramento(s) por memória insuficiente."
                    ] + list(resource_health.get("reasons") or [])

                result = task.get("result") if isinstance(task.get("result"), dict) else {}
                merged = dict(result or {})
                previous = merged.get("runtime_telemetry") if isinstance(merged.get("runtime_telemetry"), dict) else {}
                stage = str(merged.get("pipeline_stage") or task.get("message") or "processing")
                signature = f"{int(task.get('progress') or 0)}|{stage}|{str(task.get('message') or '')}"
                now_iso = datetime.now(timezone.utc).isoformat()
                stage_changed_at = previous.get("stage_changed_at")
                if signature != str(previous.get("stage_signature") or "") or not stage_changed_at:
                    stage_changed_at = now_iso
                sequence = int(previous.get("sequence") or sequence or 0) + 1
                runtime_telemetry = {
                    "version": 1,
                    "heartbeat_at": now_iso,
                    "stage_changed_at": stage_changed_at,
                    "stage_signature": signature,
                    "stage": stage,
                    "sequence": sequence,
                    "executor_id": executor_id,
                    "pid": os.getpid(),
                    "resource_health": resource_health,
                    "oom_kill_baseline": int(baseline_oom_kills or 0),
                    "oom_kill_delta": oom_kill_delta,
                    "monitor_errors": monitor_errors,
                    "last_monitor_error": last_monitor_error,
                }
                merge_task_result(task_id, {"runtime_telemetry": runtime_telemetry})
            except Exception as exc:
                monitor_errors += 1
                last_monitor_error = f"{type(exc).__name__}: {exc}"[:300]
            if stop_event.wait(interval):
                break

    thread = threading.Thread(
        target=_monitor,
        name=f"video-runtime-{str(task_id)[:8]}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _prefer_renderer_as_oom_victim(payload: Dict[str, Any], task_id: str) -> None:
    """Prioriza encerrar o renderizador, nunca a API, se o kernel ficar sem RAM."""
    if not (
        bool((payload or {}).get("_isolated_renderer"))
        or _requires_isolated_video_process(payload, task_id)
    ):
        return
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="utf-8") as handle:
            handle.write("500")
    except Exception:
        pass


def _start_isolated_video_generation(
    payload: Dict[str, Any],
    task_id: str,
    *,
    start_message: str = "Iniciando geração em processo isolado...",
) -> bool:
    protected_process = _requires_isolated_video_process(payload, task_id)
    try:
        configured_method = str(os.getenv("VIDEO_GENERATION_PROCESS_START_METHOD") or "spawn").strip().lower()
        method = configured_method if configured_method in {"spawn", "forkserver", "fork"} else "spawn"
        if sys.platform == "win32":
            method = "spawn"
        ctx = multiprocessing.get_context(method)
        child_payload = dict(payload or {})
        child_payload["_isolated_renderer"] = True
        proc = ctx.Process(
            target=process_video_generation_payload,
            args=(child_payload, task_id),
            daemon=True,
        )
        proc.start()
        update_task(
            task_id,
            status="processing",
            progress=1,
            message=start_message,
            result=_dispatch_task_result(task_id, payload, "process", pid=proc.pid),
        )

        def _watch(child: multiprocessing.Process, tid: str):
            try:
                child.join()
                if child.exitcode and child.exitcode != 0:
                    task = get_task(tid) or {}
                    status = str(task.get("status") or "").lower()
                    if status not in {"completed", "failed", "cancelled", "paused", "pause_requested"}:
                        killed_by_system = int(child.exitcode or 0) < 0
                        suffix = (
                            " O renderizador foi encerrado pelo sistema, provavelmente por falta de memória."
                            if killed_by_system
                            else " O processo isolado terminou com erro."
                        )
                        update_task(
                            tid,
                            status="failed",
                            progress=int(task.get("progress") or 0),
                            message=(
                                "A produção não foi concluída, mas a API permaneceu protegida."
                                f"{suffix} Código de saída: {child.exitcode}."
                            ),
                            result=_dispatch_task_result(
                                tid,
                                payload,
                                "process_interrupted",
                                retryable=True,
                                likely_oom=bool(killed_by_system),
                                exit_code=int(child.exitcode or 0),
                            ),
                        )
            except Exception:
                pass

        threading.Thread(target=_watch, args=(proc, task_id), daemon=True).start()
        return True
    except Exception as exc:
        if protected_process:
            update_task(
                task_id,
                status="failed",
                progress=0,
                message=(
                    "Não foi possível iniciar o renderizador isolado; a execução dentro da API foi bloqueada "
                    f"por segurança. Detalhe: {str(exc)[:200]}"
                ),
                result=_dispatch_task_result(
                    task_id,
                    payload,
                    "process_start_failed",
                    retryable=True,
                ),
            )
        return False

def _dispatch_video_generation_task(payload: Dict[str, Any], task_id: str):
    """Enfileira vídeo pesado no RQ e nunca cai para execução local em produção.

    O servidor principal (CPX22) pode coordenar/monitorar a fila, mas a geração
    pesada pertence ao worker dedicado (CX33). Se Redis/RQ/worker estiver
    indisponível, a tarefa é preservada como pendente em vez de iniciar thread ou
    processo local.
    """
    payload = _maybe_enable_render_only_flags(payload, task_id)
    requires_isolation = _requires_isolated_video_process(payload, task_id)
    resource_report = _series_resource_preflight(payload, task_id)
    if resource_report is not None and not bool(resource_report.get("allowed")):
        return

    app_env = str(os.getenv("APP_ENV") or "").strip().lower()
    production = app_env in {"production", "prod"}
    use_rq_raw = str(os.getenv("USE_RQ_FOR_VIDEO_GENERATION") or "").strip()
    if use_rq_raw:
        use_rq = use_rq_raw.lower() in {"1", "true", "yes", "on"}
    else:
        use_rq = conn is not None and _rq_workers_online()

    current = get_task(task_id) or {}
    try:
        current_progress = max(0, min(100, int(current.get("progress") or 0)))
    except Exception:
        current_progress = 0
    preserved_progress = max(1, current_progress)

    worker_online = bool(conn is not None and _rq_workers_online())
    # Um worker dedicado registrado sempre tem prioridade. Uma variável antiga
    # USE_RQ_FOR_VIDEO_GENERATION=false não pode desviar produção pesada para
    # o app principal quando o CX33 está disponível.
    if worker_online:
        try:
            rq_queue.enqueue(
                process_video_generation_payload,
                payload,
                task_id,
                job_timeout=_rq_video_timeout_seconds(),
            )
            update_task(
                task_id,
                status="processing",
                progress=preserved_progress,
                message="Enfileirado no worker de vídeo CX33; aguardando/confirmando execução...",
                result=_dispatch_task_result(task_id, payload, "rq"),
            )
            return
        except Exception as exc:
            if production:
                update_task(
                    task_id,
                    status="pending",
                    progress=preserved_progress,
                    message=(
                        "Falha ao enfileirar no worker CX33/RQ. A tarefa foi preservada e NÃO será "
                        f"executada no servidor principal. Detalhe: {str(exc)[:180]}"
                    ),
                    result=_dispatch_task_result(
                        task_id,
                        payload,
                        "rq_enqueue_failed",
                        retryable=True,
                    ),
                )
                return

    if production:
        update_task(
            task_id,
            status="pending",
            progress=preserved_progress,
            message=(
                "Worker de vídeo CX33/RQ indisponível. A produção foi preservada e NÃO será "
                "executada no servidor principal. Restabeleça o worker e reinicie/retome a tarefa."
            ),
            result=_dispatch_task_result(
                task_id,
                payload,
                "rq_worker_unavailable",
                retryable=True,
            ),
        )
        return

    # Fallback local mantido apenas para desenvolvimento/homologação explícita.
    allow_inline_raw = os.getenv("ALLOW_INLINE_VIDEO_GENERATION")
    # Fail-closed por padrão: execução pesada local só existe quando um ambiente
    # de desenvolvimento/homologação habilita explicitamente esta variável.
    if allow_inline_raw is None or not str(allow_inline_raw).strip():
        allow_inline = False
    else:
        allow_inline = str(allow_inline_raw).strip().lower() in {"1", "true", "yes", "on"}
    if not allow_inline:
        update_task(
            task_id,
            status="pending",
            progress=preserved_progress,
            message="Aguardando worker RQ; execução local desativada.",
        )
        return

    executor = (os.getenv("VIDEO_GENERATION_EXECUTOR") or "thread").strip().lower()
    if executor not in {"auto", "thread", "process"}:
        executor = "thread"
    if requires_isolation:
        executor = "process"

    use_process = executor == "process" and (conn is not None or requires_isolation)
    if use_process:
        if _start_isolated_video_generation(payload, task_id):
            return
        if requires_isolation:
            return

    update_task(
        task_id,
        status="processing",
        progress=preserved_progress,
        message="Iniciando geração local de desenvolvimento...",
        result=_dispatch_task_result(task_id, payload, "thread"),
    )
    thread = threading.Thread(target=process_video_generation_payload, args=(payload, task_id), daemon=True)
    thread.start()

def _kick_story_video_task_queue() -> Optional[str]:
    db = SessionLocal()
    try:
        rows = _load_story_video_task_rows(db, limit=100)
        if not rows:
            _kick_primary_production_queue_async()
            return None
        cleanup_info = _cleanup_story_video_task_queue(db, rows=rows)
        if cleanup_info.get("changed"):
            rows = _load_story_video_task_rows(db, limit=100)
            if not rows:
                _kick_primary_production_queue_async()
                return None
        processing = next(
            (r for r in rows if str(r.status or "").lower() in {"processing", "pause_requested"}),
            None,
        )
        if processing:
            return processing.id
        if _is_video_factory_busy():
            return None
        pending = next((r for r in rows if str(r.status or "").lower() == "pending"), None)
        if not pending:
            _kick_primary_production_queue_async()
            return None
        result_obj = None
        if pending.result_json:
            try:
                result_obj = json.loads(pending.result_json)
            except Exception:
                result_obj = None
        payload = _video_task_result_payload(result_obj)
        if not payload:
            pending.status = "failed"
            pending.message = "Payload inválido para geração de vídeo."
            db.commit()
            _kick_primary_production_queue_async()
            return None
    finally:
        db.close()
    try:
        _dispatch_video_generation_task(payload, pending.id)
    except Exception as e:
        try:
            update_task(pending.id, status="pending", progress=0, message=f"Fila aguardando inicialização. ({str(e)[:200]})")
        except Exception:
            pass
    return pending.id

def _kick_story_video_task_queue_async():
    def _run_safely():
        try:
            _kick_story_video_task_queue()
        except Exception as exc:
            print(f"Aviso ao avançar fila canônica de vídeos: {type(exc).__name__}: {str(exc)[:200]}")

    threading.Thread(target=_run_safely, daemon=True).start()


def _kick_primary_production_queue() -> Optional[int]:
    """Libera o próximo vídeo da fila principal usando o runner já existente."""
    db = SessionLocal()
    pending_job_id = None
    try:
        if db.query(Job).filter(Job.status == "processing").first():
            return None
        factory = VideoFactory(db)
        factory._enqueue_next_long_video()
        pending_jobs = (
            db.query(Job)
            .filter(Job.status == "pending")
            .order_by(Job.created_at.asc(), Job.id.asc())
            .limit(100)
            .all()
        )
        for job in pending_jobs:
            video = getattr(job, "video", None)
            status = _normalize_video_status(getattr(video, "status", None)) if video else ""
            if status not in {"PAUSED", "CANCELLED"}:
                pending_job_id = int(job.id)
                break
    finally:
        db.close()
    if pending_job_id is not None:
        process_jobs_background()
    return pending_job_id


def _kick_primary_production_queue_async():
    def _run_safely():
        try:
            _kick_primary_production_queue()
        except Exception as exc:
            print(f"Aviso ao avançar fila principal de vídeos: {type(exc).__name__}: {str(exc)[:200]}")

    threading.Thread(target=_run_safely, daemon=True).start()


def _apply_youtube_auto_editorial_intelligence(
    db: Optional[Session],
    script: Any,
    *,
    ai_service: Any,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    plan = dict(script) if isinstance(script, dict) else {}
    if not plan:
        return plan
    settings_row = get_latest_settings(db) if db is not None else None
    settings_payload = normalize_editorial_intelligence_settings(
        serialize_official_factory_settings(settings_row)
    )
    helper = EditorialIntelligenceService(CinematicQualityService(ai_service=ai_service))
    result = helper.review_plan(
        plan,
        settings_payload,
        task_id=str(task_id or "").strip() or None,
    )
    updates = result.get("plan_updates") if isinstance(result, dict) and isinstance(result.get("plan_updates"), dict) else {}
    if not updates:
        return plan

    # ``review_plan`` retorna o plano completo quando a revisão é aplicada,
    # porém os fluxos disabled/skipped/dry-run retornam somente o bloco de
    # auditoria ``editorial_intelligence``. Substituir o plano inteiro por
    # esse bloco apaga título, descrição e cenas antes da renderização.
    merged_plan = dict(plan)
    merged_plan.update(updates)
    return merged_plan

def _require_user_from_query_token(token: Optional[str], db: Session) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from jose import JWTError, jwt
        payload = jwt.decode(token, _AUTH_SECRET_KEY, algorithms=[_AUTH_ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Not authenticated")
    except Exception:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(User.email == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def process_jobs_background():
    """Background task to process video generation jobs. Um vídeo por vez."""
    db = SessionLocal()
    redis_lock = None
    file_lock = None
    try:
        if conn:
            try:
                redis_lock = conn.lock(FACTORY_LOCK_KEY, timeout=4 * 60 * 60, blocking_timeout=1)
                if not redis_lock.acquire(blocking=False):
                    return  # Outro worker já está processando
            except Exception as e:
                print(f"Error acquiring Redis factory lock: {e}")
                redis_lock = None

        # Sem Redis: usar file lock para garantir 1 job por vez (evita múltiplos processando)
        if not conn or not redis_lock:
            try:
                file_lock = FileLock(_FACTORY_LOCK_PATH, timeout=0)
                file_lock.acquire()
            except Timeout:
                return  # Outro processo já está processando
            except Exception as e:
                print(f"Error acquiring file factory lock: {e}")

        if _rq_workers_online():
            return

        factory = VideoFactory(db)
        factory.process_next_job()
    except Exception as e:
        print(f"Error processing background job: {e}")
    finally:
        if redis_lock:
            try:
                redis_lock.release()
            except Exception:
                pass
        if file_lock:
            try:
                file_lock.release()
            except Exception:
                pass
        try:
            _kick_story_video_task_queue_async()
        except Exception:
            pass
        db.close()

def _resolve_video_file_path(raw_path: Optional[str]) -> str:
    """
    Resolve path robusto para arquivos de vídeo, cobrindo:
    - path absoluto salvo no banco
    - path relativo legado
    - URL /media/videos/... ou /static/videos/...
    """
    if not raw_path:
        return ""

    value = str(raw_path).strip()
    if not value:
        return ""
    # Normaliza separador e remove query/hash legados
    value = value.replace("\\", "/").split("?", 1)[0].split("#", 1)[0].strip()
    if not value:
        return ""

    candidates: List[str] = []
    if os.path.isabs(value):
        candidates.append(value)

    # Relativo ao cwd atual (legado)
    candidates.append(os.path.abspath(value))

    try:
        from app.config import absolute_path_for_video, STATIC_DIR
        candidates.append(absolute_path_for_video(value))
        name = os.path.basename(value)
        if name:
            candidates.append(os.path.join("/data", "media", "videos", name))
            candidates.append(str(STATIC_DIR / "videos" / name))
            candidates.append(os.path.join("/app", "static", "videos", name))  # legado em alguns deploys
    except Exception:
        pass

    checked = set()
    for path in candidates:
        if not path or path in checked:
            continue
        checked.add(path)
        if os.path.exists(path) and os.path.isfile(path):
            return path
    return ""


def _resolve_rendered_video_file_path(video_result: Any) -> str:
    """Resolve o MP4 recém-renderizado sem confundir URL pública com path do disco.

    ``VideoGeneratorService`` devolve ambos ``file_path`` (arquivo real) e
    ``video_url`` (URL servida pelo FastAPI). Em produção, uma URL como
    ``/media/videos/x.mp4`` não é o arquivo ``/media/videos/x.mp4``: o arquivo
    fica em ``/data/media/videos/x.mp4``. A validação final deve, portanto,
    priorizar o path informado pelo renderizador e usar a URL apenas como
    fallback compatível com resultados antigos.
    """
    result = video_result if isinstance(video_result, dict) else {}
    render_report = result.get("render_report")
    if not isinstance(render_report, dict):
        render_report = {}

    candidates = [
        result.get("file_path"),
        render_report.get("file_path"),
        result.get("video_url"),
        render_report.get("video_url"),
    ]
    for candidate in candidates:
        resolved = _resolve_video_file_path(candidate)
        if resolved:
            return resolved

    # Mantém um path útil no diagnóstico quando o renderizador informou um
    # arquivo que realmente não existe, sem transformar URL em raiz do SO.
    reported_file = str(result.get("file_path") or render_report.get("file_path") or "").strip()
    if reported_file and not reported_file.startswith(("http://", "https://")):
        return reported_file if os.path.isabs(reported_file) else os.path.abspath(reported_file)

    video_url = str(result.get("video_url") or render_report.get("video_url") or "").strip()
    if video_url and not video_url.startswith(("http://", "https://")):
        try:
            from app.config import absolute_path_for_video
            return absolute_path_for_video(video_url)
        except Exception:
            return ""
    return ""

def _normalize_video_url_for_client(raw_url: Optional[str]) -> Optional[str]:
    """Normaliza URLs legadas/paths absolutos para URL pública reproduzível no browser."""
    if not raw_url:
        return raw_url

    value = str(raw_url).strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/media/videos/") or value.startswith("/static/videos/"):
        return value

    try:
        from app.config import VIDEO_URL_PREFIX
        resolved = _resolve_video_file_path(value)
        name = os.path.basename(resolved) if resolved else os.path.basename(value)
        if name:
            return f"{VIDEO_URL_PREFIX}/{name}"
    except Exception:
        pass
    return value

def _video_range_response(request: Request, filepath: str, inline_filename: Optional[str] = None):
    range_header = request.headers.get("range")
    file_size = os.path.getsize(filepath)
    headers: Dict[str, str] = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if inline_filename:
        headers["Content-Disposition"] = f'inline; filename="{inline_filename}"'

    if not range_header:
        return FileResponse(filepath, media_type="video/mp4", headers=headers)

    try:
        units, rng = range_header.split("=", 1)
        if units.strip().lower() != "bytes":
            return FileResponse(filepath, media_type="video/mp4", headers=headers)
        start_s, end_s = (rng.split("-", 1) + [""])[:2]
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
    except Exception:
        return FileResponse(filepath, media_type="video/mp4", headers=headers)

    def _iterfile(path: str, start_pos: int, end_pos: int, chunk_size: int = 1024 * 1024):
        with open(path, "rb") as f:
            f.seek(start_pos)
            remaining = end_pos - start_pos + 1
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    content_length = end - start + 1
    headers = {
        **headers,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(content_length),
    }
    return StreamingResponse(_iterfile(filepath, start, end), status_code=206, media_type="video/mp4", headers=headers)

def _latest_final_asset_path(db: Session, video_id: int) -> str:
    """Retorna o caminho existente do asset FINAL mais recente do vídeo."""
    assets = (
        db.query(Asset)
        .filter(Asset.video_id == video_id, Asset.kind == "FINAL")
        .order_by(Asset.created_at.desc(), Asset.id.desc())
        .all()
    )
    for asset in assets:
        resolved = _resolve_video_file_path(asset.storage_key)
        if resolved:
            return resolved
    return ""

def _normalize_video_status(value: Optional[str]) -> str:
    """Normaliza status de vídeo para evitar divergência de caixa/espaços/legado."""
    raw = (value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    aliases = {
        "COMPLETED": "READY",
        "FAILED": "ERROR",
    }
    return aliases.get(upper, upper)

def _progress_from_video_status(status: Optional[str]) -> int:
    """Fallback de progresso quando o job ativo não reportou progresso ainda."""
    s = _normalize_video_status(status)
    mapping = {
        "QUEUED": 5,
        "SCRIPT": 25,
        "TTS": 45,
        "VISUALS": 65,
        "RENDER": 85,
        "READY": 100,
        "PUBLISHED": 100,
        "PAUSED": 0,
        "CANCELLED": 0,
        "ERROR": 0,
    }
    return mapping.get(s, 0)

def _last_log_line(logs: Optional[str], max_len: int = 220) -> str:
    """Extrai a última linha útil dos logs para exibir status curto na UI."""
    text = (logs or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    msg = lines[-1]
    if len(msg) > max_len:
        msg = msg[: max_len - 3] + "..."
    return msg

def _is_mock_upload(upload_result: Any) -> bool:
    return isinstance(upload_result, dict) and upload_result.get("status") == "uploaded_mock"

def _extract_uploaded_youtube_id(upload_result: Any) -> Optional[str]:
    candidate = None
    if isinstance(upload_result, dict):
        if upload_result.get("error") or _is_mock_upload(upload_result):
            return None
        candidate = (
            upload_result.get("id")
            or upload_result.get("videoId")
            or upload_result.get("youtube_video_id")
        )
    else:
        candidate = upload_result
    value = str(candidate or "").strip()
    if not value or value in {"{}", "None"}:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", value):
        return None
    return value

def _build_youtube_watch_url(youtube_video_id: Optional[str]) -> Optional[str]:
    video_id = str(youtube_video_id or "").strip()
    if not video_id:
        return None
    return f"https://www.youtube.com/watch?v={video_id}"

def _serialize_upload_result(upload_result: Any) -> Dict[str, Any]:
    if isinstance(upload_result, dict):
        return dict(upload_result)
    raw = str(upload_result or "").strip()
    return {"raw": raw} if raw else {}

def _publish_error_message(upload_result: Any, action_label: str = "publicar") -> str:
    """Mensagem amigável e consistente para falhas de upload no YouTube."""
    if _is_mock_upload(upload_result):
        return "Canal não conectado ao YouTube. Configure as credenciais em Configurações antes de publicar."
    if isinstance(upload_result, dict):
        raw = (upload_result.get("error") or "").strip()
        if raw:
            return raw
    raw = str(upload_result or "").strip()
    if raw and raw not in {"{}", "None"}:
        return raw
    return (
        f"Falha ao {action_label} no YouTube. Verifique as credenciais em Configurações "
        f"ou variáveis YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN."
    )

def _append_upload_error_note(description: Optional[str], message: str) -> str:
    note = f"[UPLOAD_ERRO]: {message}"
    current = (description or "").strip()
    if note in current:
        return current
    if current:
        return f"{current}\n\n{note}"
    return note

def _infer_resume_step(db: Session, video: Video) -> Optional[str]:
    """Infere próxima etapa para retomar produção após pausa."""
    paused_or_pending = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status.in_(["paused", "pending"]))
        .order_by(Job.created_at.asc(), Job.id.asc())
        .first()
    )
    if paused_or_pending:
        return (paused_or_pending.step or "").strip().lower() or "script"

    latest_completed = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "completed")
        .order_by(Job.created_at.desc(), Job.id.desc())
        .first()
    )
    if latest_completed:
        step = (latest_completed.step or "").strip().lower()
        next_map = {
            "script": "tts",
            "tts": "visuals",
            "visuals": "render",
        }
        return next_map.get(step)

    status = _normalize_video_status(video.status)
    from_status = {
        "QUEUED": "script",
        "SCRIPT": "tts",
        "TTS": "visuals",
        "VISUALS": "render",
    }
    return from_status.get(status, "script")

def _build_public_video_url_from_path(resolved_path: Optional[str]) -> Optional[str]:
    """Converte path físico do vídeo para URL pública servida pela API."""
    if not resolved_path:
        return None
    name = os.path.basename(str(resolved_path).strip())
    if not name:
        return None
    try:
        from app.config import VIDEO_URL_PREFIX
        return f"{VIDEO_URL_PREFIX}/{name}"
    except Exception:
        return f"/media/videos/{name}"

def _find_scheduled_mirror_by_source(db: Session, production_video_id: int) -> Optional[ScheduledVideo]:
    """Encontra item em scheduled_videos criado a partir do vídeo de produção."""
    candidates = (
        db.query(ScheduledVideo)
        .filter(ScheduledVideo.script_data.isnot(None))
        .filter(ScheduledVideo.script_data.contains("source_production_video_id"))
        .all()
    )
    for item in candidates:
        try:
            data = json.loads(item.script_data or "{}")
            if str(data.get("source_production_video_id")) == str(production_video_id):
                return item
        except Exception:
            continue
    return None

def _build_scheduled_mirror_index(db: Session) -> Dict[str, ScheduledVideo]:
    """Indexa scheduled_videos espelhados por source_production_video_id."""
    index: Dict[str, ScheduledVideo] = {}
    candidates = (
        db.query(ScheduledVideo)
        .filter(ScheduledVideo.script_data.isnot(None))
        .filter(ScheduledVideo.script_data.contains("source_production_video_id"))
        .all()
    )
    for item in candidates:
        try:
            data = json.loads(item.script_data or "{}")
            source_id = data.get("source_production_video_id")
            if source_id is not None:
                index[str(source_id)] = item
        except Exception:
            continue
    return index


_SCHEDULED_AUTO_ALLOWED_SOURCES = {"manual_schedule", "schedule_plan", "scheduled_automation"}
_SCHEDULED_AUTO_BLOCKED_SOURCES = {"generated_story", "production_queue", "derived_short", "derived_from_scheduled"}


def _load_scheduled_video_payload(video: Optional[ScheduledVideo]) -> Dict[str, Any]:
    if not video or not getattr(video, "script_data", None):
        return {}
    try:
        data = json.loads(video.script_data or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _scheduled_video_processing_policy(
    video: Optional[ScheduledVideo],
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else _load_scheduled_video_payload(video)
    source = str(data.get("source") or "").strip().lower()
    status = str(getattr(video, "status", "") or "").strip().lower()
    explicit_flag = data.get("auto_processing_eligible")
    has_rendered_asset = bool(
        str(data.get("video_url") or "").strip()
        or str(getattr(video, "video_url", "") or "").strip()
    )
    has_source_production = bool(str(data.get("source_production_video_id") or "").strip())

    policy = {
        "source": source or "legacy_schedule",
        "auto_process_eligible": False,
        "reason": "scheduled_video_source_not_allowed",
    }

    if status in {"completed", "ready", "published", "awaiting_publish"}:
        policy["reason"] = "scheduled_video_already_finished"
        return policy

    if has_source_production or source in _SCHEDULED_AUTO_BLOCKED_SOURCES:
        policy["reason"] = "scheduled_video_source_blocked_from_auto_run"
        return policy

    if explicit_flag is not None:
        policy["auto_process_eligible"] = bool(explicit_flag)
        policy["reason"] = (
            "scheduled_video_explicitly_allowed"
            if bool(explicit_flag)
            else "scheduled_video_explicitly_blocked"
        )
        return policy

    if source in _SCHEDULED_AUTO_ALLOWED_SOURCES:
        policy["auto_process_eligible"] = True
        policy["reason"] = "scheduled_video_known_automation_source"
        return policy

    if not source and not has_rendered_asset and status in {"queued", "processing", "failed", "pending"}:
        policy["auto_process_eligible"] = True
        policy["reason"] = "scheduled_video_legacy_queue_item"
        return policy

    if has_rendered_asset:
        policy["reason"] = "scheduled_video_already_has_rendered_asset"
    return policy


def _normalize_scheduled_equivalence_value(value: Any, default: str = "") -> str:
    normalized = re.sub(r"\s+", " ", str(value or default).strip()).lower()
    return normalized or str(default or "").strip().lower()


def _build_scheduled_video_equivalence_key(
    *,
    user_id: Optional[int],
    theme: Any,
    scheduled_for: Optional[datetime],
    video_type: Any,
) -> str:
    safe_schedule = ""
    if isinstance(scheduled_for, datetime):
        safe_schedule = scheduled_for.replace(second=0, microsecond=0).isoformat()
    return "|".join(
        [
            str(int(user_id)) if user_id is not None else "anonymous",
            _normalize_scheduled_equivalence_value(theme, "geral"),
            safe_schedule,
            _normalize_scheduled_equivalence_value(video_type, "video"),
        ]
    )


def _acquire_scheduled_video_creation_lock(db: Session, equivalence_key: str) -> None:
    if not equivalence_key:
        return
    try:
        lock_key = int(hashlib.sha256(equivalence_key.encode("utf-8")).hexdigest()[:16], 16)
        if lock_key >= (1 << 63):
            lock_key -= (1 << 64)
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
    except Exception:
        # Ambiente não-PostgreSQL ou driver sem suporte: segue com o check defensivo em aplicação.
        pass


def _find_active_equivalent_scheduled_video(
    db: Session,
    *,
    user_id: Optional[int],
    theme: Any,
    scheduled_for: Optional[datetime],
    video_type: Any,
) -> Optional[ScheduledVideo]:
    if not isinstance(scheduled_for, datetime):
        return None

    query = db.query(ScheduledVideo).filter(
        ScheduledVideo.scheduled_for == scheduled_for.replace(second=0, microsecond=0),
        ScheduledVideo.status.in_(_SCHEDULED_VIDEO_ACTIVE_STATUSES),
    )
    if user_id is None:
        query = query.filter(ScheduledVideo.user_id == None)
    else:
        query = query.filter(ScheduledVideo.user_id == int(user_id))

    expected_theme = _normalize_scheduled_equivalence_value(theme, "geral")
    expected_type = _normalize_scheduled_equivalence_value(video_type, "video")
    for candidate in query.order_by(ScheduledVideo.id.asc()).all():
        if _normalize_scheduled_equivalence_value(getattr(candidate, "theme", None), "geral") != expected_theme:
            continue
        if _normalize_scheduled_equivalence_value(getattr(candidate, "video_type", None), "video") != expected_type:
            continue
        return candidate
    return None

def _upsert_scheduled_from_production(db: Session, video: Video, mirror_index: Optional[Dict[str, ScheduledVideo]] = None):
    """Garante que vídeo READY/PUBLISHED da produção apareça na fila de aguardando publicação."""
    norm_status = _normalize_video_status(video.status)
    if norm_status not in {"READY", "PUBLISHED"}:
        return

    plan = video.plan
    final_path = _latest_final_asset_path(db, video.id) or _resolve_video_file_path(video.youtube_video_id)
    public_video_url = _normalize_video_url_for_client(_build_public_video_url_from_path(final_path)) if final_path else None

    if mirror_index is not None:
        mirror = mirror_index.get(str(video.id))
    else:
        mirror = _find_scheduled_mirror_by_source(db, video.id)
    payload = {}
    if mirror and mirror.script_data:
        try:
            payload = json.loads(mirror.script_data)
        except Exception:
            payload = {}
    payload.update({
        "source": "production_queue",
        "source_production_video_id": video.id,
        "production_status": norm_status,
        "auto_processing_eligible": False,
        "processing_mode": "publish_only",
    })

    target_status = "published" if norm_status == "PUBLISHED" else "completed"
    target_type = "short" if (video.type or "").upper() == "SHORT" else "video"
    target_scheduled_for = video.scheduled_at or (mirror.scheduled_for if mirror else None) or datetime.now()

    if mirror:
        mirror.theme = (plan.theme if plan and getattr(plan, "theme", None) else mirror.theme) or "Produção"
        mirror.title = video.title or mirror.title or f"Vídeo {video.id}"
        mirror.description = video.description or mirror.description or ""
        mirror.scheduled_for = target_scheduled_for
        mirror.status = target_status
        mirror.progress = 100
        mirror.video_type = target_type
        mirror.voice_style = getattr(plan, "voice_style", None) or mirror.voice_style or "human"
        mirror.voice_gender = getattr(plan, "voice_gender", None) or mirror.voice_gender or "female"
        if public_video_url:
            mirror.video_url = public_video_url
        if norm_status == "PUBLISHED" and video.youtube_video_id:
            mirror.youtube_video_id = video.youtube_video_id
            mirror.uploaded_at = mirror.uploaded_at or datetime.now()
        mirror.script_data = json.dumps(payload)
    else:
        mirror = ScheduledVideo(
            theme=(plan.theme if plan and getattr(plan, "theme", None) else "Produção"),
            title=video.title or f"Vídeo {video.id}",
            description=video.description or "",
            scheduled_for=target_scheduled_for,
            status=target_status,
            video_type=target_type,
            script_data=json.dumps(payload),
            video_url=public_video_url,
            progress=100,
            auto_post=False,
            voice_style=getattr(plan, "voice_style", "human") if plan else "human",
            voice_gender=getattr(plan, "voice_gender", "female") if plan else "female",
            music_file_path=getattr(plan, "music_file", None) if plan else None,
            youtube_video_id=(video.youtube_video_id if norm_status == "PUBLISHED" else None),
            uploaded_at=(datetime.now() if norm_status == "PUBLISHED" else None),
        )
        db.add(mirror)
        if mirror_index is not None:
            mirror_index[str(video.id)] = mirror

def _sync_ready_production_to_scheduled(db: Session, limit: int = 200):
    """Sincroniza vídeos READY/PUBLISHED da produção para a fila de aguardando publicação."""
    from sqlalchemy import func
    candidates = (
        db.query(Video)
        .filter(func.upper(func.trim(Video.status)).in_(["READY", "PUBLISHED", "COMPLETED"]))
        .order_by(Video.created_at.desc())
        .limit(limit)
        .all()
    )
    mirror_index = _build_scheduled_mirror_index(db)
    for video in candidates:
        _upsert_scheduled_from_production(db, video, mirror_index=mirror_index)
    db.commit()


def _load_json_object(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_scheduled_unified_mirror_index(db: Session) -> Dict[str, ScheduledVideo]:
    """Indexa espelhos do pipeline canônico sem depender apenas do JSON legado."""
    from sqlalchemy import or_

    rows = (
        db.query(ScheduledVideo)
        .filter(
            or_(
                ScheduledVideo.unified_video_id.isnot(None),
                ScheduledVideo.task_id.isnot(None),
                ScheduledVideo.script_data.contains("unified_video_id"),
                ScheduledVideo.script_data.contains("task_id"),
            )
        )
        .order_by(ScheduledVideo.id.desc())
        .limit(5000)
        .all()
    )
    index: Dict[str, ScheduledVideo] = {}
    for row in rows:
        payload = _load_json_object(getattr(row, "script_data", None))
        unified_id = getattr(row, "unified_video_id", None) or payload.get("unified_video_id")
        task_id = getattr(row, "task_id", None) or payload.get("task_id")
        video_url = _normalize_video_url_for_client(
            getattr(row, "video_url", None) or payload.get("video_url")
        )
        if unified_id:
            index.setdefault(f"unified:{unified_id}", row)
        if task_id:
            index.setdefault(f"task:{task_id}", row)
        if video_url:
            index.setdefault(f"url:{video_url}", row)
    return index


def _upsert_scheduled_from_unified(
    db: Session,
    unified: UnifiedVideo,
    mirror_index: Optional[Dict[str, ScheduledVideo]] = None,
) -> Optional[ScheduledVideo]:
    """Espelha um UnifiedVideo pronto na lista de publicação, de forma idempotente."""
    normalized_status = str(getattr(unified, "status", "") or "").strip().lower()
    ready_statuses = {
        UnifiedVideoStatus.AWAITING_REVIEW,
        UnifiedVideoStatus.APPROVED,
        UnifiedVideoStatus.PUBLISHED,
    }
    if normalized_status not in ready_statuses:
        return None

    script = _load_json_object(getattr(unified, "script_json", None))
    result = _load_json_object(getattr(unified, "result_json", None))
    raw_video_url = (
        getattr(unified, "video_url", None)
        or result.get("video_url")
        or _build_public_video_url_from_path(getattr(unified, "video_path", None))
    )
    public_video_url = _normalize_video_url_for_client(raw_video_url)

    index = mirror_index if mirror_index is not None else _build_scheduled_unified_mirror_index(db)
    mirror = index.get(f"unified:{unified.id}")
    if mirror is None and getattr(unified, "task_id", None):
        mirror = index.get(f"task:{unified.task_id}")
    if mirror is None and public_video_url:
        mirror = index.get(f"url:{public_video_url}")

    title = str(
        result.get("title")
        or script.get("title")
        or getattr(unified, "topic", None)
        or f"Vídeo {unified.id}"
    ).strip()
    description = str(result.get("description") or script.get("description") or "").strip()
    content_type = str(
        result.get("kind")
        or getattr(unified, "content_type", None)
        or "devotional"
    ).strip().lower()
    video_type = str(result.get("video_type") or "video").strip().lower()
    if video_type not in {"video", "short"}:
        video_type = "video"

    previous_payload = _load_json_object(getattr(mirror, "script_data", None)) if mirror else {}
    previous_payload.update(
        {
            "source": "unified_video_pipeline",
            "unified_video_id": int(unified.id),
            "task_id": str(unified.task_id or ""),
            "kind": content_type,
            "video_type": video_type,
            "video_url": public_video_url,
            "video_path": getattr(unified, "video_path", None),
            "auto_processing_eligible": False,
            "processing_mode": "publish_only",
        }
    )

    target_status = "published" if normalized_status == UnifiedVideoStatus.PUBLISHED else "completed"
    if mirror is None:
        mirror = ScheduledVideo(
            user_id=getattr(unified, "user_id", None),
            theme="História/Devocional",
            title=title,
            description=description,
            scheduled_for=getattr(unified, "created_at", None) or datetime.now(),
            status=target_status,
            video_type=video_type,
            script_data=json.dumps(previous_payload, ensure_ascii=False),
            video_url=public_video_url,
            task_id=getattr(unified, "task_id", None),
            unified_video_id=int(unified.id),
            video_path=getattr(unified, "video_path", None),
            progress=100,
            auto_post=False,
            pipeline="unified_video_pipeline",
            youtube_video_id=getattr(unified, "youtube_video_id", None),
            youtube_url=getattr(unified, "youtube_url", None),
            uploaded_at=getattr(unified, "published_at", None),
        )
        db.add(mirror)
    else:
        mirror.user_id = getattr(unified, "user_id", None) or mirror.user_id
        mirror.title = title or mirror.title
        mirror.description = description or mirror.description or ""
        if str(getattr(mirror, "status", "") or "").strip().lower() != "published":
            mirror.status = target_status
        mirror.video_type = video_type
        mirror.progress = 100
        mirror.task_id = getattr(unified, "task_id", None) or mirror.task_id
        mirror.unified_video_id = int(unified.id)
        mirror.video_path = getattr(unified, "video_path", None) or mirror.video_path
        mirror.pipeline = "unified_video_pipeline"
        if public_video_url:
            mirror.video_url = public_video_url
        if getattr(unified, "youtube_video_id", None):
            mirror.youtube_video_id = unified.youtube_video_id
        if getattr(unified, "youtube_url", None):
            mirror.youtube_url = unified.youtube_url
        if getattr(unified, "published_at", None):
            mirror.uploaded_at = unified.published_at
        mirror.script_data = json.dumps(previous_payload, ensure_ascii=False)

    index[f"unified:{unified.id}"] = mirror
    if getattr(unified, "task_id", None):
        index[f"task:{unified.task_id}"] = mirror
    if public_video_url:
        index[f"url:{public_video_url}"] = mirror
    return mirror


def _sync_ready_unified_to_scheduled(db: Session, limit: int = 500) -> None:
    """Faz backfill dos UnifiedVideo prontos, inclusive após navegador fechado/deploy."""
    candidates = (
        db.query(UnifiedVideo)
        .filter(
            UnifiedVideo.status.in_(
                [
                    UnifiedVideoStatus.AWAITING_REVIEW,
                    UnifiedVideoStatus.APPROVED,
                    UnifiedVideoStatus.PUBLISHED,
                ]
            )
        )
        .order_by(UnifiedVideo.created_at.desc(), UnifiedVideo.id.desc())
        .limit(limit)
        .all()
    )
    mirror_index = _build_scheduled_unified_mirror_index(db)
    for unified in candidates:
        _upsert_scheduled_from_unified(db, unified, mirror_index=mirror_index)
    db.commit()

def _delete_scheduled_mirror(db: Session, production_video_id: int):
    """Remove item espelho em scheduled_videos de um vídeo de produção."""
    mirror = _find_scheduled_mirror_by_source(db, production_video_id)
    if mirror:
        db.delete(mirror)

router = APIRouter(
    prefix="/youtube",
    tags=["youtube"],
    responses={404: {"description": "Not found"}},
)

try:
    from app.services.unified_video_pipeline import (
        UnifiedVideoPipelineService,
        UnifiedVideoRequest,
        build_unified_video_request,
        unified_video_pipeline,
    )
except Exception as _unified_import_err:  # pragma: no cover - fallback gracefully deploys
    UnifiedVideoPipelineService = None  # type: ignore[assignment,misc]
    UnifiedVideoRequest = None  # type: ignore[assignment,misc]
    build_unified_video_request = None  # type: ignore[assignment]
    unified_video_pipeline = None  # type: ignore[assignment,misc]
    _UNIFIED_IMPORT_ERR: Optional[str] = str(_unified_import_err)
else:
    _UNIFIED_IMPORT_ERR: Optional[str] = None


def _unified_enabled() -> bool:
    if _UNIFIED_IMPORT_ERR:
        return False
    return bool(
        unified_video_pipeline is not None
        and UnifiedVideoRequest is not None
        and build_unified_video_request is not None
    )


def _build_unified_request_from_legacy(payload: Dict[str, Any], user_id: Optional[int] = None, module: str = "story") -> Optional["UnifiedVideoRequest"]:
    """Compatibilidade local; a normalização canônica vive no serviço central."""
    if not _unified_enabled() or build_unified_video_request is None:
        return None
    try:
        return build_unified_video_request(
            payload,
            source_module=str(module or payload.get("source_module") or "story"),
            user_id=user_id,
        )
    except Exception:
        return None


def _safe_hash(payload: Any) -> str:
    try:
        import hashlib as _h
        js = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return _h.sha256(js.encode("utf-8")).hexdigest()[:32]
    except Exception:
        return os.urandom(8).hex()


def _json_dump_short(data: Any, max_chars: int = 500) -> str:
    try:
        s = json.dumps(data, ensure_ascii=False)
        if len(s) <= int(max_chars):
            return s
        return s[: int(max_chars)] + f"... (+{len(s) - int(max_chars)} chars)"
    except Exception as exc:
        return f"[json_error:{type(exc).__name__}]"

# --- Video Factory Models & Endpoints ---

class PlanRequest(BaseModel):
    mode: str = "theme" # theme | music
    theme: Optional[str] = None
    music_file: Optional[str] = None
    days: int = 7
    videos_per_day: int = 1
    shorts_per_day: int = 1
    duration_min: int = 8
    voice_style: str = "human"
    voice_gender: str = "female"
    start_date: str  # YYYY-MM-DD

@router.post("/auto/plans")
def create_content_plan(plan: PlanRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Cria um novo plano de conteúdo e enfileira a geração."""
    # TODO: Get user_id from auth (using 1 for now as placeholder if no auth)
    user_id = 1 
    
    factory = VideoFactory(db)
    new_plan = factory.create_plan(plan.dict(), user_id=user_id)
    
    # Trigger processing in background (MVP without Redis for now)
    background_tasks.add_task(process_jobs_background)
    
    return {"status": "Plan created", "plan_id": new_plan.id, "message": "Vídeos enfileirados para produção."}

@router.get("/auto/plans/{plan_id}")
def get_content_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(ContentPlan).filter(ContentPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan

@router.get("/auto/stats")
def get_production_stats(db: Session = Depends(get_db)):
    """Retorna contagem de vídeos/plans para diagnóstico (ex: vídeos sumiram após deploy)."""
    from sqlalchemy import func
    total_videos = db.query(Video).count()
    total_plans = db.query(ContentPlan).count()
    by_status = (
        db.query(func.upper(func.trim(Video.status)).label("s"), func.count(Video.id))
        .group_by(func.upper(func.trim(Video.status)))
        .all()
    )
    return {
        "total_videos": total_videos,
        "total_plans": total_plans,
        "videos_by_status": {s or "null": c for s, c in by_status},
    }

def _reset_stuck_jobs(db: Session, timeout_minutes: int = 10):
    """Reseta Jobs travados em 'processing' há muito tempo (ex: servidor reiniciou)."""
    from datetime import timedelta
    from sqlalchemy import func
    cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
    stuck = (
        db.query(Job)
        .filter(Job.status == "processing")
        .filter(func.coalesce(Job.updated_at, Job.created_at) < cutoff)
        .all()
    )
    for j in stuck:
        j.status = "pending"
        j.progress = 0
        j.logs = (j.logs or "") + f"\n[Recovery] Job travado por {timeout_minutes}+ min. Reenfileirado em {datetime.now()}."
        v = db.query(Video).get(j.video_id)
        if v and (v.status or "").upper() not in ("PAUSED", "CANCELLED", "CANCELED"):
            v.status = "queued"
        print(f"[Factory] Recovery: Job {j.id} (video {j.video_id}) reenfileirado.")
    if stuck:
        db.commit()

@router.get("/auto/queue")
def get_production_queue(background_tasks: BackgroundTasks, status: Optional[str] = None, limit: int = 50, db: Session = Depends(get_db)):
    """Retorna a fila de produção (vídeos e jobs). Dispara processamento se houver jobs pendentes."""
    VideoFactory(db).sync_canonical_videos()
    _reset_stuck_jobs(db)
    pending = db.query(Job).filter(Job.status == "pending").first()
    processing = db.query(Job).filter(Job.status == "processing").first()
    if pending and not processing:
        background_tasks.add_task(process_jobs_background)
    query = db.query(Video).order_by(Video.scheduled_at.asc())
    
    if status:
        from sqlalchemy import func
        normalized_status = (status or "").strip().upper()
        query = query.filter(func.upper(func.trim(Video.status)) == normalized_status)
        
    videos = query.limit(limit).all()
    
    result = []
    for v in videos:
        normalized_video_status = _normalize_video_status(v.status)
        # Prioridade: job em processamento > pendente > último job
        processing_job = (
            db.query(Job)
            .filter(Job.video_id == v.id, Job.status == "processing")
            .order_by(Job.created_at.desc())
            .first()
        )
        pending_job = (
            db.query(Job)
            .filter(Job.video_id == v.id, Job.status == "pending")
            .order_by(Job.created_at.desc())
            .first()
        )
        latest_job = (
            db.query(Job)
            .filter(Job.video_id == v.id)
            .order_by(Job.created_at.desc())
            .first()
        )
        active_job = processing_job or pending_job or latest_job

        fallback_progress = _progress_from_video_status(normalized_video_status)
        job_progress = int(active_job.progress or 0) if active_job else 0
        # Quando há job em processamento, usar progresso real (evita travar em 85% do fallback RENDER)
        if active_job and active_job.status == "processing" and job_progress > 0:
            progress = job_progress
        else:
            progress = max(job_progress, fallback_progress)

        if normalized_video_status == "PAUSED":
            current_step = "paused"
        elif normalized_video_status == "CANCELLED":
            current_step = "cancelled"
        elif active_job:
            if active_job.status == "processing":
                current_step = active_job.step or "processing"
            elif active_job.status == "pending":
                current_step = active_job.step or "queued"
            else:
                current_step = active_job.step or "queued"
        else:
            current_step = "queued"

        status_message = _last_log_line(active_job.logs if active_job else "")
        if not status_message:
            if active_job and active_job.status == "processing":
                status_message = f"Processando etapa: {active_job.step or 'produção'}..."
            elif active_job and active_job.status == "pending":
                status_message = f"Aguardando início da etapa: {active_job.step or 'produção'}."
            elif normalized_video_status == "QUEUED":
                status_message = "Aguardando vez na fila de produção."
            elif normalized_video_status == "PAUSED":
                status_message = "Produção pausada pelo usuário."
            elif normalized_video_status == "CANCELLED":
                status_message = "Produção cancelada pelo usuário."

        result.append({
            "id": v.id,
            "title": v.title,
            "type": v.type,
            "status": normalized_video_status,
            "created_at": v.created_at,
            "scheduled_at": v.scheduled_at,
            "progress": progress,
            "current_step": current_step,
            "status_message": status_message,
            "logs": active_job.logs if active_job else "",
            "youtube_id": v.youtube_video_id,
            "task_id": getattr(v, "task_id", None),
            "unified_video_id": getattr(v, "unified_video_id", None),
            "pipeline": getattr(v, "pipeline", None),
        })
    
    return result

@router.post("/videos/{video_id}/retry")
def retry_video_step(video_id: int, step: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Reinicia uma etapa específica para um vídeo com erro."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Reset status para permitir reprocessamento (vídeos em erro ficam travados)
    if (video.status or "").upper() in {"ERROR", "FAILED"}:
        video.status = "queued"

    # Mapeia nomes do frontend para steps do VideoFactory
    raw_step = (step or "").strip().lower()
    step_map = {
        "script_generate": "script",
        "queued": "script",
        "error": "script",
    }
    factory_step = step_map.get(raw_step, raw_step)
    valid_steps = {"script", "tts", "visuals", "render", "shorts_extract"}
    if factory_step not in valid_steps:
        factory_step = "script"

    factory = VideoFactory(db)
    factory._add_job(video.id, factory_step)
    db.commit()

    background_tasks.add_task(process_jobs_background)

    return {"status": "Job added", "step": factory_step}

@router.post("/videos/{video_id}/publish")
def publish_video(video_id: int, db: Session = Depends(get_db)):
    """Publica o vídeo no YouTube (Integração real)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    # Check if video is ready
    if (video.status or "").upper() != "READY":
        raise HTTPException(status_code=400, detail="Video is not ready for publication")
        
    # Get Final Asset
    final_path = _latest_final_asset_path(db, video.id)
    if not final_path:
        # Compatibilidade com registros legados que salvaram path em youtube_video_id
        final_path = _resolve_video_file_path(video.youtube_video_id)
    if not final_path:
        raise HTTPException(status_code=500, detail="Video file not found")
        
    # Call YouTube Service (real; quando não conectado, service retorna mock id)
    try:
        tags: List[str] = []
        if video.tags:
            tags = [t.strip() for t in str(video.tags).split(",") if t.strip()]

        service = YouTubeService()
        upload_result = service.upload_video(
            final_path,
            title=video.title or f"Vídeo {video.id}",
            description=video.description or "Vídeo gerado automaticamente por Codexia.",
            tags=tags
        )

        youtube_id = _extract_uploaded_youtube_id(upload_result)

        if not youtube_id:
            # Falha de publicação não deve destruir estado READY do vídeo gerado.
            # Isso permite corrigir credenciais e tentar publicar novamente sem reprocessar.
            video.status = "READY"
            err_msg = _publish_error_message(upload_result, action_label="publicar")
            video.description = _append_upload_error_note(video.description, err_msg)
            db.commit()
            raise HTTPException(status_code=502, detail=err_msg)
        
        video.status = "PUBLISHED"
        video.published_at = datetime.now()
        video.youtube_video_id = youtube_id
        _upsert_scheduled_from_production(db, video)
        db.commit()
        
        return {"status": "Published", "youtube_id": youtube_id}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/videos/{video_id}/schedule")
def schedule_production_video(video_id: int, data: Dict[str, Any], db: Session = Depends(get_db)):
    """Atualiza data/hora agendada de publicação para vídeo da fila de produção."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    dt_raw = (data.get("scheduled_at") or data.get("scheduled_for") or "").strip()
    if not dt_raw:
        raise HTTPException(status_code=400, detail="Data de agendamento não informada.")

    try:
        try:
            scheduled_at = datetime.fromisoformat(dt_raw)
        except Exception:
            scheduled_at = datetime.strptime(dt_raw, "%Y-%m-%dT%H:%M")
    except Exception:
        raise HTTPException(status_code=400, detail="Formato de data inválido.")

    video.scheduled_at = scheduled_at
    _upsert_scheduled_from_production(db, video)
    db.commit()
    return {
        "status": "scheduled",
        "id": video.id,
        "scheduled_at": video.scheduled_at.isoformat() if video.scheduled_at else None,
    }

@router.post("/videos/{video_id}/pause")
def pause_production_video(video_id: int, db: Session = Depends(get_db)):
    """Pausa a produção de um vídeo (cooperativo entre etapas)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    status = _normalize_video_status(video.status)
    if status in {"READY", "PUBLISHED"}:
        raise HTTPException(status_code=400, detail="Vídeo já concluído/publicado; não há produção para pausar.")
    if status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Vídeo cancelado não pode ser pausado.")
    if status == "PAUSED":
        return {"status": "paused", "message": "Produção já está pausada."}

    pending_jobs = db.query(Job).filter(Job.video_id == video.id, Job.status == "pending").all()
    for j in pending_jobs:
        j.status = "paused"
        j.logs = (j.logs or "") + "Pausa solicitada pelo usuário.\n"

    processing_job = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "processing")
        .order_by(Job.created_at.desc())
        .first()
    )
    if processing_job:
        processing_job.logs = (processing_job.logs or "") + "Pausa solicitada pelo usuário (aplicada após a etapa atual).\n"

    video.status = "PAUSED"
    db.commit()
    _kick_story_video_task_queue_async()
    return {"status": "paused", "message": "Produção pausada com sucesso."}

@router.post("/videos/{video_id}/resume")
def resume_production_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Retoma a produção de um vídeo pausado."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    status = _normalize_video_status(video.status)
    if status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Vídeo cancelado não pode ser retomado.")
    if status != "PAUSED":
        raise HTTPException(status_code=400, detail="Apenas vídeos pausados podem ser retomados.")

    processing_job = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "processing")
        .order_by(Job.created_at.desc())
        .first()
    )
    if processing_job:
        # Caso raro: pausa solicitada e retomada quase simultânea.
        video.status = (processing_job.step or "processing").upper()
        db.commit()
        return {"status": "processing", "message": "Vídeo já estava em processamento."}

    paused_jobs = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "paused")
        .order_by(Job.created_at.asc(), Job.id.asc())
        .all()
    )
    if paused_jobs:
        for j in paused_jobs:
            j.status = "pending"
            j.logs = (j.logs or "") + "Produção retomada pelo usuário.\n"

    next_step = _infer_resume_step(db, video)
    if not next_step:
        # Não há etapa restante: marca como pronto.
        video.status = "READY"
        _upsert_scheduled_from_production(db, video)
        db.commit()
        return {"status": "ready", "message": "Vídeo já estava concluído."}

    video.status = "queued"
    db.commit()

    factory = VideoFactory(db)
    factory._add_job(video.id, next_step)
    background_tasks.add_task(_kick_story_video_task_queue)
    return {
        "status": "queued",
        "step": next_step,
        "message": "Produção retomada e recolocada na fila.",
    }

@router.post("/videos/{video_id}/cancel")
def cancel_production_video(video_id: int, db: Session = Depends(get_db)):
    """Cancela a produção de um vídeo."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    status = _normalize_video_status(video.status)
    if status in {"READY", "PUBLISHED"}:
        raise HTTPException(status_code=400, detail="Vídeo já concluído/publicado; não é possível cancelar produção.")
    if status == "CANCELLED":
        return {"status": "cancelled", "message": "Produção já estava cancelada."}

    queued_jobs = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status.in_(["pending", "paused"]))
        .all()
    )
    for j in queued_jobs:
        j.status = "cancelled"
        j.logs = (j.logs or "") + "Produção cancelada pelo usuário.\n"

    processing_job = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "processing")
        .order_by(Job.created_at.desc())
        .first()
    )
    if processing_job:
        processing_job.logs = (processing_job.logs or "") + "Cancelamento solicitado pelo usuário (aplicado após a etapa atual).\n"

    video.status = "CANCELLED"
    db.commit()
    _kick_story_video_task_queue_async()
    return {"status": "cancelled", "message": "Produção cancelada com sucesso."}

@router.post("/videos/{video_id}/regenerate")
def regenerate_production_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Refaz um vídeo da fila de produção desde a etapa de script."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Remove vídeos derivados (shorts) para refazer pipeline limpo
    children = db.query(Video).filter(Video.parent_video_id == video.id).all()
    for child in children:
        child_assets = db.query(Asset).filter(Asset.video_id == child.id).all()
        for asset in child_assets:
            path = _resolve_video_file_path(asset.storage_key)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f"Erro ao remover asset do derivado {path}: {e}")
        db.delete(child)

    # Remove arquivos físicos já gerados do vídeo principal
    assets = db.query(Asset).filter(Asset.video_id == video.id).all()
    for asset in assets:
        path = _resolve_video_file_path(asset.storage_key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Erro ao remover asset antigo {path}: {e}")

    # Limpa entidades derivadas do pipeline para recomeçar do zero
    db.query(Job).filter(Job.video_id == video.id).delete(synchronize_session=False)
    db.query(Scene).filter(Scene.video_id == video.id).delete(synchronize_session=False)
    db.query(Asset).filter(Asset.video_id == video.id).delete(synchronize_session=False)

    video.status = "queued"
    video.youtube_video_id = None
    video.published_at = None
    _delete_scheduled_mirror(db, video.id)
    db.commit()

    factory = VideoFactory(db)
    factory._add_job(video.id, "script")
    background_tasks.add_task(process_jobs_background)
    return {"status": "queued", "message": "Vídeo reenfileirado para regeneração."}

@router.delete("/videos/{video_id}")
def delete_production_video(video_id: int, db: Session = Depends(get_db)):
    """Exclui um vídeo da fila de produção, removendo assets e derivados."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Remove arquivos físicos dos assets
    assets = db.query(Asset).filter(Asset.video_id == video.id).all()
    for asset in assets:
        path = _resolve_video_file_path(asset.storage_key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Erro ao remover arquivo de asset {path}: {e}")

    # Remove vídeos derivados (shorts) para evitar órfãos
    children = db.query(Video).filter(Video.parent_video_id == video.id).all()
    for child in children:
        _delete_scheduled_mirror(db, child.id)
        db.delete(child)

    _delete_scheduled_mirror(db, video.id)
    db.delete(video)
    db.commit()
    return {"status": "deleted"}

@router.get("/videos/{video_id}")
def get_video_details(video_id: int, db: Session = Depends(get_db)):
    """Retorna detalhes do vídeo e jobs para a fila de produção."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    jobs = db.query(Job).filter(Job.video_id == video_id).order_by(Job.created_at.desc()).all()
    return {
        "id": video.id,
        "title": video.title,
        "status": video.status,
        "jobs": [{"step": j.step, "status": j.status, "logs": j.logs or ""} for j in jobs]
    }

@router.get("/videos/{video_id}/download")
def download_video(video_id: int, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Download do arquivo de vídeo final (para fila de produção)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    final_path = _latest_final_asset_path(db, video.id)
    if not final_path:
        # Compatibilidade com registros legados que salvaram path em youtube_video_id
        final_path = _resolve_video_file_path(video.youtube_video_id)
    if not final_path:
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(final_path, media_type="video/mp4", filename=os.path.basename(final_path))

@router.get("/videos/{video_id}/watch")
def watch_video(video_id: int, request: Request, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Abre/streama o vídeo final da fila de produção no navegador."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    final_path = _latest_final_asset_path(db, video.id)
    if not final_path:
        final_path = _resolve_video_file_path(video.youtube_video_id)
    if not final_path:
        raise HTTPException(status_code=404, detail="Video file not found")

    inline_name = os.path.basename(final_path) or f"video_{video_id}.mp4"
    return _video_range_response(request, final_path, inline_filename=inline_name)

@router.get("/schedule/{video_id}/watch")
def watch_scheduled_video(video_id: int, request: Request, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    path = _resolve_video_file_path(video.video_url)
    if not path:
        raise HTTPException(status_code=404, detail="Video file not found")
    inline_name = os.path.basename(path) or f"video_{video_id}.mp4"
    return _video_range_response(request, path, inline_filename=inline_name)

@router.post("/auto/process-job")
def trigger_process_job(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Manually trigger job processing (for testing/worker simulation)."""
    background_tasks.add_task(process_jobs_background)
    return {"status": "Processing triggered"}

@router.post("/auto/unblock")
def unblock_production_queue(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Reseta jobs travados em processing (5+ min) e dispara processamento. Use quando a fila travar."""
    _reset_stuck_jobs(db, timeout_minutes=5)
    background_tasks.add_task(process_jobs_background)
    return {"status": "ok", "message": "Fila desbloqueada. Processamento disparado."}

@router.post("/upload-music")
async def upload_music(file: UploadFile = File(...)):
    upload_dir = Path("app/static/music_uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize filename
    safe_filename = "".join([c for c in file.filename if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
    file_path = upload_dir / f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_filename}"
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Return absolute path for internal usage or relative for client if needed
    # Using absolute path for backend processing consistency
    return {"file_path": str(file_path.absolute()), "filename": safe_filename}

class VideoRequest(BaseModel):
    topic: Optional[str] = None
    duration: int = 5
    auto_upload: bool = False
    mode: str = "topic" # topic | story
    kind: Optional[str] = None  # story | devotional | prayer (apenas quando mode=story)
    story_content: Optional[str] = None
    custom_image_paths: Optional[List[str]] = None
    selected_images: Optional[List[str]] = None
    seeded_script: Optional[Dict[str, Any]] = None
    reuse_audio_from: Optional[Dict[str, Any]] = None
    music_file_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    override_title: Optional[str] = None
    override_description: Optional[str] = None
    override_tags: Optional[List[str]] = None
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None
    image_mode: Optional[str] = None  # auto | single | multiple
    aspect_ratio: Optional[str] = "16:9"
    idempotency_key: Optional[str] = None
    force_regenerate: bool = False
    force_reuse_assets: bool = False
    force_render_only: bool = False
    editorial_reviewed: bool = False
    editorial_review_ready: bool = False

class StoryTextGenerateRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    instruction: str
    duration_min: int = 10
    duration_max: Optional[int] = None

class StoryTextImproveRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    instruction: str = ""
    original_text: str
    duration_min: int = 10
    duration_max: Optional[int] = None
class StoryImagesRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    story_content: str
    count: int = 4
    aspect_ratio: str = "16:9"  # 16:9 | 9:16
    image_mode: Optional[str] = None  # single | multiple

class StoryShortsRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    story_content: str
    count: int = 3
    selected_images: Optional[List[str]] = None
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None

class StoryDraftSaveRequest(BaseModel):
    title: Optional[str] = None
    kind: str = "story"
    content: str
    metadata: Optional[Dict[str, Any]] = None

class CreateShortsFromScheduledRequest(BaseModel):
    count: int = 3
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None

class ContentFactoryGenerateRequest(BaseModel):
    idea: str
    channel_name: Optional[str] = None

class ContentFactoryRegenerateThumbnailRequest(BaseModel):
    text: Optional[str] = None

def _generate_story_images_payload(request: StoryImagesRequest, progress_callback=None) -> Dict[str, Any]:
    ai_service = AIContentGenerator()
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional", "prayer"}:
        kind = "story"
    allow_image_reuse = kind == "prayer"
    image_mode = (request.image_mode or "").strip().lower()
    if image_mode not in {"single", "multiple"}:
        image_mode = "single" if kind == "prayer" else "multiple"

    try:
        count = int(request.count or 1)
    except Exception:
        count = 1
    if image_mode == "single":
        count = 1
    count = max(1, min(12, count))

    aspect_ratio = (request.aspect_ratio or "16:9").strip()
    if aspect_ratio not in {"16:9", "9:16"}:
        aspect_ratio = "16:9"

    story_content = (request.story_content or "").strip()
    if not story_content:
        raise HTTPException(status_code=400, detail="story_content é obrigatório.")
    story_content = story_content[:8000]

    def _progress(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    def _extract_scene_chunks(text: str, n: int) -> List[str]:
        raw = (text or "").replace("\r\n", "\n").strip()
        if not raw:
            return []
        parts = [p.strip() for p in raw.split("\n") if p.strip()]
        if len(parts) < n:
            import re
            sents = re.split(r"(?<=[.!?])\s+", raw.replace("\n", " ").strip())
            parts = [s.strip() for s in sents if s and s.strip()]
        if not parts:
            return []
        chunks: List[str] = []
        idx = 0
        max_chars = 420
        while idx < len(parts) and len(chunks) < n:
            buf = parts[idx].strip()
            idx += 1
            while idx < len(parts) and len(buf) < int(max_chars * 0.7):
                cand = parts[idx].strip()
                if not cand:
                    idx += 1
                    continue
                if len(buf) + 1 + len(cand) > max_chars:
                    break
                buf = f"{buf} {cand}"
                idx += 1
            chunks.append(buf[:max_chars].strip())
        while len(chunks) < n:
            chunks.append(chunks[-1])
        return chunks[:n]

    _progress(5, "Preparando cenas para gerar imagens...")
    scene_chunks = _extract_scene_chunks(story_content, count)
    if not scene_chunks:
        base = story_content.replace("\n", " ").strip()[:320]
        scene_chunks = [base] * count

    prompts: List[str] = []
    _progress(8, "Gerando prompts de imagem por cena...")
    for idx, chunk in enumerate(scene_chunks[:count]):
        try:
            previous_chunk = scene_chunks[idx - 1] if idx > 0 and idx - 1 < len(scene_chunks) else ""
            p_list = ai_service.generate_story_image_prompts(
                chunk,
                n=1,
                kind=kind,
                story_context=story_content,
                story_title="YouTube Auto Biblical Story",
                scene_number=idx + 1,
                previous_scene_text=previous_chunk,
            ) or []
            p = (p_list[0] if isinstance(p_list, list) and p_list else "") if p_list is not None else ""
        except Exception:
            p = ""
        p = (p or "").strip()
        if not p:
            safe_kind = "story" if kind == "story" else ("devotional" if kind == "devotional" else "prayer reflection")
            p = (
                f"Photorealistic cinematic photography of a scene inspired by this {safe_kind} excerpt: {chunk}. "
                "Natural lighting, pleasant mood, realistic humans (no dolls), proportional anatomy, avoid close-up portraits. "
                "No horror, no zombies, no monsters, no gore, no blood, no creepy, no uncanny. "
                "No text, no watermark, no logo."
            )
        prompts.append(p[:900])

    covers_dir = Path("app/static/covers")
    covers_dir.mkdir(parents=True, exist_ok=True)

    images: List[Dict[str, Any]] = []

    all_prompts = prompts[:count]

    total = max(1, len(all_prompts))
    for idx, p in enumerate(all_prompts):
        step_pct = 15 + int((idx / total) * 80)
        prompt_text = (p or "").strip()
        if not prompt_text:
            continue
        try:
            def _status(msg: str, scene_idx=idx, total_scenes=count, pct=step_pct):
                _progress(pct, f"Imagem {scene_idx+1}/{total}: {msg}")

            _progress(step_pct, f"Gerando imagem {idx+1}/{total}...")
            url = ai_service.generate_image(prompt_text, aspect_ratio=aspect_ratio, providers=["openai_direct"], status_callback=_status)
        except Exception:
            url = None
        if not url:
            if allow_image_reuse and images:
                reused = dict(images[-1])
                reused["prompt"] = prompt_text
                reused["reused"] = True
                images.append(reused)
                continue
            raise HTTPException(status_code=503, detail="Não foi possível gerar a imagem com OpenAI. Verifique a chave da API, saldo/créditos e modelo disponível.")
        images.append({"url": url, "prompt": prompt_text})

    if not images:
        raise HTTPException(
            status_code=503,
            detail="Não foi possível gerar a imagem com OpenAI. Verifique a chave da API, saldo/créditos e modelo disponível."
        )

    _progress(100, "Imagens prontas.")
    return {"count": len(images), "images": images, "kind": kind, "aspect_ratio": aspect_ratio, "image_mode": image_mode}

class ImageBankItem(BaseModel):
    url: str
    prompt: Optional[str] = None

class ImageBankSaveRequest(BaseModel):
    selected_images: List[ImageBankItem]
    kind: Optional[str] = None
    aspect_ratio: Optional[str] = None

@router.post("/images/bank/save")
def save_images_to_bank(request: ImageBankSaveRequest, db: Session = Depends(get_db)):
    items = request.selected_images or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="selected_images é obrigatório.")
    kind = (request.kind or "").strip().lower()
    if kind and kind not in {"story", "devotional", "prayer"}:
        kind = ""
    aspect = (request.aspect_ratio or "").strip()
    static_root = os.path.abspath(os.path.join("app", "static"))
    bank_dir = os.path.join(static_root, "image_bank")
    os.makedirs(bank_dir, exist_ok=True)
    saved = []
    for it in items:
        url = (it.url or "").strip()
        if not url or not url.startswith("/static/"):
            continue
        rel = url.replace("/static/", "", 1).replace("/", os.sep)
        src = os.path.abspath(os.path.join(static_root, rel))
        if not os.path.exists(src):
            continue
        ext = os.path.splitext(src)[1] or ".png"
        filename = f"bank_{uuid.uuid4().hex}{ext}"
        dst = os.path.abspath(os.path.join(bank_dir, filename))
        try:
            shutil.copyfile(src, dst)
        except Exception:
            continue
        image_url = f"/static/image_bank/{filename}"
        aiimg = AIImage(
            theme=(kind or "image"),
            style=(aspect or None),
            prompt=(it.prompt or None),
            image_url=image_url,
        )
        try:
            db.add(aiimg)
            db.commit()
            db.refresh(aiimg)
            saved.append({"id": aiimg.id, "url": image_url, "prompt": aiimg.prompt})
        except Exception as e:
            db.rollback()
            try:
                os.remove(dst)
            except Exception:
                pass
    if not saved:
        raise HTTPException(status_code=400, detail="Nenhuma imagem válida para salvar.")
    return {"saved": saved}

@router.get("/images/bank")
def list_image_bank(
    aspect_ratio: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    limit: int = Query(40, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(AIImage).order_by(AIImage.created_at.desc())
    if aspect_ratio:
        q = q.filter(AIImage.style == aspect_ratio)
    if kind:
        q = q.filter(AIImage.theme == kind)
    rows = q.limit(limit).all()
    return [{"id": r.id, "url": r.image_url, "prompt": r.prompt, "style": r.style, "theme": r.theme} for r in rows]

@router.delete("/images/bank/{image_id}")
def delete_image_bank_item(image_id: int, db: Session = Depends(get_db)):
    row = db.query(AIImage).filter(AIImage.id == image_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Imagem não encontrada.")
    url = (row.image_url or "").strip()
    static_root = os.path.abspath(os.path.join("app", "static"))
    if url.startswith("/static/"):
        rel = url.replace("/static/", "", 1).replace("/", os.sep)
        path = os.path.abspath(os.path.join(static_root, rel))
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    db.delete(row)
    db.commit()
    return {"message": "Imagem removida."}
def _generate_story_shorts_payload(request: StoryShortsRequest, progress_callback=None) -> Dict[str, Any]:
    from app.redis_client import conn
    from filelock import FileLock, Timeout
    lock_path = _FACTORY_LOCK_PATH
    lock_key = FACTORY_LOCK_KEY

    redis_lock = None
    file_lock = None
    if conn:
        try:
            redis_lock = conn.lock(lock_key, timeout=2 * 60 * 60, blocking_timeout=1)
            if not redis_lock.acquire(blocking=False):
                raise HTTPException(status_code=409, detail="Já existe uma geração de vídeo em andamento. Aguarde terminar.")
        except HTTPException:
            raise
        except Exception:
            redis_lock = None

    if not conn or not redis_lock:
        try:
            file_lock = FileLock(lock_path, timeout=0)
            file_lock.acquire()
        except Timeout:
            raise HTTPException(status_code=409, detail="Já existe uma geração de vídeo em andamento. Aguarde terminar.")
        except Exception:
            file_lock = None

    try:
        ai_service = AIContentGenerator()
        kind = (request.kind or "story").strip().lower()
        if kind not in {"story", "devotional", "prayer"}:
            kind = "story"

        try:
            count = int(request.count or 1)
        except Exception:
            count = 1
        count = max(1, min(8, count))

        story_content = (request.story_content or "").strip()
        if not story_content:
            raise HTTPException(status_code=400, detail="story_content é obrigatório.")
        story_content = story_content[:12000]

        selected_images = []
        if request.selected_images and isinstance(request.selected_images, list):
            for v in request.selected_images:
                if isinstance(v, str) and v.strip():
                    selected_images.append(v.strip())
        selected_images = selected_images[:24]

        voice_style = (request.voice_style or "").strip() or None
        voice_gender = (request.voice_gender or "").strip() or None

        def _progress(pct: int, msg: str):
            if progress_callback:
                try:
                    progress_callback(pct, msg)
                except Exception:
                    pass

        angles = (
            ["Gancho forte (início da história)", "Momento mais impactante", "Lição final e CTA"]
            if kind == "story"
            else ["Gancho de fé (início)", "Aplicação prática", "Mensagem final e CTA"]
        )

        shorts = []
        for idx in range(count):
            angle = angles[idx % len(angles)]
            _progress(5 + int((idx / max(1, count)) * 75), f"Gerando short {idx+1}/{count} ({angle})...")
            prompt = (
                f"Crie UM roteiro de YouTube Short vertical (30-60s), baseado nesta {('história' if kind == 'story' else 'mensagem/devocional')}.\n"
                f"Foco: {angle}.\n\n"
                f"TEXTO BASE:\n{story_content}\n\n"
                "Regras: gancho no início, 3 a 5 cenas, frases curtas, sem texto na imagem."
            )
            plan = ai_service.generate_short_script_from_prompt(prompt)
            if not isinstance(plan, dict):
                plan = {"title": f"Short {idx+1}", "scenes": [{"text": "Assista até o fim.", "image_prompt": "cinematic inspiring scene"}]}
            try:
                plan["disable_scene_text_split"] = True
                plan["video_type"] = "short"
                t = str(plan.get("title") or "").strip()
                if len(t) > 60:
                    plan["title"] = t[:60].rstrip()
                desc = str(plan.get("description") or "").strip()
                if desc and "#shorts" not in desc.lower():
                    plan["description"] = (desc + "\n\n#shorts").strip()[:1200]
                elif not desc:
                    plan["description"] = "#shorts"

                raw_scenes = plan.get("scenes")
                cleaned = []
                if isinstance(raw_scenes, list):
                    for s in raw_scenes:
                        if not isinstance(s, dict):
                            continue
                        txt = str(s.get("text") or "").strip()
                        if not txt:
                            continue
                        ip = str(s.get("image_prompt") or s.get("visual_prompt") or "").strip()
                        cleaned.append({"text": txt[:180], "image_prompt": ip})
                if len(cleaned) > 5:
                    cleaned = cleaned[:5]
                while cleaned and len(cleaned) < 3:
                    cleaned.append({"text": "Inscreva-se para mais.", "image_prompt": "call to action minimal"})
                if cleaned:
                    plan["scenes"] = cleaned
            except Exception:
                pass
            if selected_images:
                plan["selected_images"] = selected_images

            child_db = SessionLocal()
            try:
                source_hash = _safe_hash({"story": story_content, "index": idx, "kind": kind, "plan": plan})
                child_payload = {
                    "source_id": f"story-short:{source_hash}:{idx}",
                    "idempotency_key": f"story-short:{source_hash}:{idx}",
                    "topic": plan.get("title") or f"Short {idx+1}",
                    "story_content": "\n\n".join(
                        str(scene.get("text") or "").strip()
                        for scene in (plan.get("scenes") or [])
                        if isinstance(scene, dict) and str(scene.get("text") or "").strip()
                    ),
                    "mode": "story",
                    "kind": "short",
                    "duration": 1,
                    "aspect_ratio": "9:16",
                    "seeded_script": plan,
                    "selected_images": selected_images or None,
                    "voice_style": voice_style,
                    "voice_gender": voice_gender,
                    "override_title": plan.get("title"),
                    "override_description": plan.get("description"),
                    "review_required": True,
                }
                child_request = build_unified_video_request(
                    child_payload,
                    source_module="story_shorts",
                    source_id=f"story-short:{source_hash}:{idx}",
                )
                child_result = unified_video_pipeline().submit_or_reuse(
                    child_db,
                    request=child_request,
                    kick_queue_callback=_kick_story_video_task_queue_async,
                    legacy_initial_result={
                        "source_module": "story_shorts",
                        "pipeline": "unified_video_pipeline",
                        "payload": child_payload,
                    },
                )
                shorts.append({
                    "title": plan.get("title") or f"Short {idx+1}",
                    "description": plan.get("description") or "",
                    "video_url": child_result.video_url,
                    "task_id": child_result.task_id,
                    "unified_video_id": child_result.unified_video_id,
                    "kind": kind,
                    "video_type": "short",
                    "pipeline": "unified_video_pipeline",
                })
            finally:
                child_db.close()

        _progress(100, "Shorts enviados ao pipeline único.")
        return {"count": len(shorts), "shorts": shorts, "kind": kind}
    finally:
        if redis_lock:
            try:
                redis_lock.release()
            except Exception:
                pass
        if file_lock:
            try:
                file_lock.release()
            except Exception:
                pass

class QueueGeneratedVideoRequest(BaseModel):
    video_url: str
    video_path: Optional[str] = None
    task_id: Optional[str] = None
    scheduled_video_id: Optional[int] = None
    unified_video_id: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None
    kind: Optional[str] = None
    video_type: Optional[str] = None
    auto_post: bool = False
    scheduled_for: Optional[str] = None
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None

@router.post("/story/generate_text")
def generate_story_text(request: StoryTextGenerateRequest):
    ai_service = AIContentGenerator()
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional", "prayer"}:
        kind = "story"
    try:
        package = generate_review_ready_story_text(
            ai_service,
            instruction=request.instruction,
            kind=kind,
            duration_min_minutes=request.duration_min,
            duration_max_minutes=request.duration_max,
        )
        return {
            **package,
            "kind": kind,
            "duration_min": request.duration_min,
            "duration_max": request.duration_max,
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "ai_text_generation_failed",
                "message": str(e)[:900],
                "kind": kind,
            },
        )

@router.post("/story/improve_text")
def improve_story_text(request: StoryTextImproveRequest):
    ai_service = AIContentGenerator()
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional", "prayer"}:
        kind = "story"
    instruction = (request.instruction or "").strip() or "Melhore o texto mantendo o sentido e aumentando a retenção."
    text = ai_service.improve_story_or_devotional_text(
        original_text=request.original_text,
        instruction=instruction,
        kind=kind,
        duration_min_minutes=request.duration_min,
        duration_max_minutes=request.duration_max,
    )
    return {"text": text, "kind": kind, "duration_min": request.duration_min, "duration_max": request.duration_max}

@router.post("/story/draft")
def save_story_draft(request: StoryDraftSaveRequest, db: Session = Depends(get_db)):
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional", "prayer"}:
        kind = "story"
    content = (request.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content é obrigatório.")
    content = content[:40000]
    meta = request.metadata if isinstance(request.metadata, dict) else {}
    title = (request.title or "").strip()
    if not title:
        base = (meta.get("instruction") or meta.get("prompt") or "").strip()
        if not base:
            base = content.split("\n", 1)[0].strip()
        title = base[:80] if base else ("História" if kind == "story" else ("Devocional" if kind == "devotional" else "Reflexão com Oração"))

    draft = StoryDraft(
        title=title,
        kind=kind,
        content=content,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    try:
        db.add(draft)
        db.commit()
        db.refresh(draft)
        return {"id": draft.id, "title": draft.title, "message": "Rascunho salvo com sucesso."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/story/drafts")
def list_story_drafts(db: Session = Depends(get_db)):
    drafts = db.query(StoryDraft).order_by(StoryDraft.updated_at.desc()).all()
    return [
        {
            "id": d.id,
            "title": d.title,
            "kind": d.kind,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        }
        for d in drafts
    ]

@router.get("/story/drafts/{draft_id}")
def get_story_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.query(StoryDraft).filter(StoryDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Rascunho não encontrado.")
    meta = json.loads(draft.metadata_json) if draft.metadata_json else {}
    return {
        "id": draft.id,
        "title": draft.title,
        "kind": draft.kind,
        "content": draft.content or "",
        "metadata": meta,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }

@router.delete("/story/drafts/{draft_id}")
def delete_story_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.query(StoryDraft).filter(StoryDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(status_code=404, detail="Rascunho não encontrado.")
    db.delete(draft)
    db.commit()
    return {"message": "Rascunho excluído."}

def _content_factory_draft_to_dict(d: StoryDraft) -> Dict[str, Any]:
    meta = json.loads(d.metadata_json) if d.metadata_json else {}
    return {
        "id": d.id,
        "title": d.title,
        "kind": d.kind,
        "content": d.content or "",
        "metadata": meta,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }

@router.post("/content-factory/generate")
def generate_content_factory_strategy(request: ContentFactoryGenerateRequest, db: Session = Depends(get_db)):
    idea = (request.idea or "").strip()
    if not idea:
        raise HTTPException(status_code=400, detail="idea é obrigatório.")

    ai = AIContentGenerator()
    channel_name = (request.channel_name or "").strip() or "Herdeiros das Promessas"
    strategy = ai.generate_youtube_content_factory_strategy(idea=idea, channel_name=channel_name)
    if not isinstance(strategy, dict) or strategy.get("error"):
        raise HTTPException(status_code=502, detail=strategy.get("error") if isinstance(strategy, dict) else "Falha ao gerar estratégia.")

    titles = strategy.get("titulos") if isinstance(strategy.get("titulos"), list) else []
    titles = [str(t).strip() for t in titles if isinstance(t, str) and t.strip()]
    selected_title = titles[0] if titles else idea[:80]

    roteiro = strategy.get("roteiro") if isinstance(strategy.get("roteiro"), dict) else {}
    blocks = {
        "gancho_0_30s": str((roteiro.get("gancho_0_30s") or "")).strip(),
        "retencao_30_120s": str((roteiro.get("retencao_30_120s") or "")).strip(),
        "corpo": str((roteiro.get("corpo") or "")).strip(),
        "cta_inscricao": str((roteiro.get("cta_inscricao") or "")).strip(),
    }
    script_text = "\n\n".join([selected_title] + [v for v in blocks.values() if v]).strip()

    seo = strategy.get("seo") if isinstance(strategy.get("seo"), dict) else {}
    tags = seo.get("tags") if isinstance(seo.get("tags"), list) else []
    tags = [str(t).strip() for t in tags if isinstance(t, str) and t.strip()]
    desc = str((seo.get("descricao") or "")).strip()
    timestamps = seo.get("timestamps") if isinstance(seo.get("timestamps"), list) else []
    timestamps = [str(t).strip() for t in timestamps if isinstance(t, str) and t.strip()]
    if timestamps:
        desc = (desc + "\n\n" + "\n".join(timestamps)).strip()

    thumb = strategy.get("thumbnail") if isinstance(strategy.get("thumbnail"), dict) else {}
    thumb_text = str((thumb.get("texto") or "")).strip() or selected_title.split(":", 1)[0].strip()[:24]
    image_prompt = str((thumb.get("imagem_prompt") or "")).strip() or None

    thumb_payload = None
    try:
        from app.services.image_storyboard_service import generate_thumbnail_with_text
        thumb_payload = generate_thumbnail_with_text(
            idea=idea,
            text=thumb_text,
            image_prompt=image_prompt,
            api_key=getattr(ai, "api_key", None),
        )
    except Exception:
        thumb_payload = None

    thumb_url = (thumb_payload.get("url") if isinstance(thumb_payload, dict) else None)
    thumb_file = (thumb_payload.get("file") if isinstance(thumb_payload, dict) else None)
    thumb_file_abs = os.path.abspath(thumb_file) if thumb_file and isinstance(thumb_file, str) else None

    meta = {
        "idea": idea,
        "channel_name": channel_name,
        "titles": titles,
        "selected_title": selected_title,
        "blocks": blocks,
        "tags": tags,
        "description": desc,
        "timestamps": timestamps,
        "thumbnail": {
            "text": thumb_text,
            "image_prompt": image_prompt,
            "url": thumb_url,
            "file_path": thumb_file_abs,
        },
        "strategy": strategy,
    }

    draft = StoryDraft(
        title=selected_title[:120],
        kind="content_factory",
        content=(script_text or "")[:40000],
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    try:
        db.add(draft)
        db.commit()
        db.refresh(draft)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    out = _content_factory_draft_to_dict(draft)
    out["titles"] = titles
    out["blocks"] = blocks
    out["tags"] = tags
    out["description"] = desc
    out["thumbnail_url"] = thumb_url
    out["thumbnail_path"] = thumb_file_abs
    return out

@router.get("/content-factory/drafts")
def list_content_factory_drafts(db: Session = Depends(get_db)):
    drafts = (
        db.query(StoryDraft)
        .filter(StoryDraft.kind == "content_factory")
        .order_by(StoryDraft.updated_at.desc())
        .all()
    )
    return [{"id": d.id, "title": d.title, "updated_at": d.updated_at.isoformat() if d.updated_at else None} for d in drafts]

@router.get("/content-factory/drafts/{draft_id}")
def get_content_factory_draft(draft_id: int, db: Session = Depends(get_db)):
    d = db.query(StoryDraft).filter(StoryDraft.id == draft_id).first()
    if not d or (d.kind or "") != "content_factory":
        raise HTTPException(status_code=404, detail="Rascunho não encontrado.")
    out = _content_factory_draft_to_dict(d)
    meta = out.get("metadata") if isinstance(out.get("metadata"), dict) else {}
    thumb = meta.get("thumbnail") if isinstance(meta.get("thumbnail"), dict) else {}
    out["titles"] = meta.get("titles") or []
    out["blocks"] = meta.get("blocks") or {}
    out["tags"] = meta.get("tags") or []
    out["description"] = meta.get("description") or ""
    out["thumbnail_url"] = thumb.get("url")
    out["thumbnail_path"] = thumb.get("file_path")
    return out

@router.post("/content-factory/drafts/{draft_id}/regenerate-thumbnail")
def regenerate_content_factory_thumbnail(draft_id: int, request: ContentFactoryRegenerateThumbnailRequest, db: Session = Depends(get_db)):
    d = db.query(StoryDraft).filter(StoryDraft.id == draft_id).first()
    if not d or (d.kind or "") != "content_factory":
        raise HTTPException(status_code=404, detail="Rascunho não encontrado.")
    meta = json.loads(d.metadata_json) if d.metadata_json else {}
    idea = str((meta.get("idea") or d.title or "")).strip()
    if not idea:
        raise HTTPException(status_code=400, detail="Rascunho sem ideia.")
    thumb_meta = meta.get("thumbnail") if isinstance(meta.get("thumbnail"), dict) else {}
    txt = (request.text or "").strip() if request and getattr(request, "text", None) else ""
    if not txt:
        txt = str((thumb_meta.get("text") or "")).strip()
    if not txt:
        txt = str((d.title or "")).strip()[:24]
    prompt = str((thumb_meta.get("image_prompt") or "")).strip() or None

    ai = AIContentGenerator()
    try:
        from app.services.image_storyboard_service import generate_thumbnail_with_text
        thumb_payload = generate_thumbnail_with_text(
            idea=idea,
            text=txt,
            image_prompt=prompt,
            api_key=getattr(ai, "api_key", None),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    file_path = os.path.abspath(str(thumb_payload.get("file") or "").strip()) if isinstance(thumb_payload, dict) else None
    url = thumb_payload.get("url") if isinstance(thumb_payload, dict) else None
    meta["thumbnail"] = {
        "text": txt,
        "image_prompt": prompt,
        "url": url,
        "file_path": file_path,
    }
    d.metadata_json = json.dumps(meta, ensure_ascii=False)
    try:
        db.add(d)
        db.commit()
        db.refresh(d)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return {"thumbnail_url": url, "thumbnail_path": file_path}

@router.post("/story/generate_images_task")
def generate_story_images_task(request: StoryImagesRequest, background_tasks: BackgroundTasks):
    task_id = create_task()
    update_task(task_id, status="processing", progress=0, message="Iniciando geração de imagens...")
    background_tasks.add_task(process_story_images_generation, request, task_id)
    return {"message": "Processo iniciado", "task_id": task_id}

@router.post("/story/generate_images")
def generate_story_images(request: StoryImagesRequest):
    return _generate_story_images_payload(request)

def process_story_images_generation(request: StoryImagesRequest, task_id: str):
    try:
        def progress_callback(progress, message):
            try:
                update_task(task_id, progress=int(progress or 0), message=message)
            except Exception:
                pass

        result = _generate_story_images_payload(request, progress_callback=progress_callback)
        update_task(task_id, progress=100, status="completed", message="Imagens geradas com sucesso!", result=result)
    except Exception as e:
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")

@router.post("/story/generate_shorts_task")
def generate_story_shorts_task(request: StoryShortsRequest, background_tasks: BackgroundTasks):
    task_id = create_task()
    update_task(task_id, status="processing", progress=0, message="Iniciando geração de shorts...")
    use_rq = conn is not None and _rq_workers_online()
    allow_inline_raw = os.getenv("ALLOW_INLINE_VIDEO_GENERATION")
    if use_rq:
        rq_queue.enqueue(process_story_shorts_generation, request.model_dump() if hasattr(request, "model_dump") else request.dict(), task_id, job_timeout=_rq_video_timeout_seconds())
    else:
        if allow_inline_raw is None or not str(allow_inline_raw).strip():
            allow_inline = True
        else:
            allow_inline = str(allow_inline_raw).strip().lower() in {"1", "true", "yes", "on"}
        if not allow_inline:
            update_task(task_id, status="failed", progress=0, message="Geração em segundo plano indisponível: inicie o worker RQ e configure REDIS_URL.")
            return {"message": "Worker indisponível", "task_id": task_id}
        background_tasks.add_task(process_story_shorts_generation, request, task_id)
    return {"message": "Processo iniciado", "task_id": task_id}

@router.post("/story/generate_shorts")
def generate_story_shorts(request: StoryShortsRequest):
    return _generate_story_shorts_payload(request)

def process_story_shorts_generation(request: Any, task_id: str):
    try:
        try:
            if isinstance(request, dict):
                request = StoryShortsRequest(**request)
        except Exception:
            pass
        def progress_callback(progress, message):
            try:
                update_task(task_id, progress=int(progress or 0), message=message)
            except Exception:
                pass

        result = _generate_story_shorts_payload(request, progress_callback=progress_callback)
        update_task(task_id, progress=100, status="completed", message="Shorts gerados com sucesso!", result=result)
        try:
            dbn = SessionLocal()
            n = SystemNotification(
                user_id=None,
                kind="shorts_generated",
                title="Shorts gerados",
                message="Shorts gerados com sucesso!",
                payload_json=json.dumps({"task_id": task_id}, ensure_ascii=False),
                status="new",
            )
            dbn.add(n)
            dbn.commit()
        except Exception:
            try:
                dbn.rollback()
            except Exception:
                pass
        finally:
            try:
                dbn.close()
            except Exception:
                pass
    except Exception as e:
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")

@router.post("/schedule/{video_id}/create_shorts_task")
def create_shorts_from_scheduled_task(video_id: int, request: CreateShortsFromScheduledRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado.")
    if (video.video_type or "video").strip().lower() != "video":
        raise HTTPException(status_code=400, detail="Apenas vídeos longos (não-shorts) podem gerar shorts.")

    task_id = create_task()
    update_task(task_id, status="processing", progress=0, message="Iniciando criação de shorts a partir do vídeo...")
    payload = {
        "count": int(getattr(request, "count", 3) or 3),
        "voice_style": (getattr(request, "voice_style", None) or None),
        "voice_gender": (getattr(request, "voice_gender", None) or None),
    }
    background_tasks.add_task(process_create_shorts_from_scheduled_video, video_id, payload, task_id)
    return {"message": "Processo iniciado", "task_id": task_id}

def process_create_shorts_from_scheduled_video(video_id: int, payload: Dict[str, Any], task_id: str):
    db = SessionLocal()
    try:
        scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
        if not scheduled:
            raise Exception("Vídeo não encontrado.")
        if (scheduled.video_type or "video").strip().lower() != "video":
            raise Exception("Apenas vídeos longos (não-shorts) podem gerar shorts.")

        try:
            count = int((payload or {}).get("count") or 3)
        except Exception:
            count = 3
        count = max(1, min(8, count))

        kind = "story"
        base_text = ""
        data = {}
        if scheduled.script_data:
            try:
                data = json.loads(scheduled.script_data or "{}") or {}
            except Exception:
                data = {}

        if isinstance(data, dict):
            raw_kind = str(data.get("kind") or "").strip().lower()
            if raw_kind in {"story", "devotional", "prayer"}:
                kind = raw_kind

            scenes = data.get("scenes")
            if isinstance(scenes, list) and scenes:
                parts = []
                for s in scenes:
                    if isinstance(s, dict):
                        t = (s.get("text") or s.get("narration_text") or s.get("narration") or "").strip()
                        if t:
                            parts.append(t)
                base_text = "\n".join(parts).strip()

            if not base_text:
                for k in ("story_content", "text", "content", "script", "concept", "narration_text", "narration"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        base_text = v.strip()
                        break

        if not base_text:
            title = (scheduled.title or "").strip()
            desc = (scheduled.description or "").strip()
            base_text = f"{title}\n\n{desc}".strip()

        base_text = (base_text or "").strip()[:12000]
        if not base_text:
            raise Exception("Sem conteúdo para gerar shorts.")

        ai_service = AIContentGenerator()
        p_voice_style = ((payload or {}).get("voice_style") or "").strip() if isinstance(payload, dict) else ""
        p_voice_gender = ((payload or {}).get("voice_gender") or "").strip() if isinstance(payload, dict) else ""
        voice_style = p_voice_style or (getattr(scheduled, "voice_style", "") or "").strip() or None
        voice_gender = p_voice_gender or (getattr(scheduled, "voice_gender", "") or "").strip() or None

        selected_images = []
        try:
            if isinstance(data, dict):
                for k in ("rendered_images", "selected_images", "images"):
                    v = data.get(k)
                    if isinstance(v, list) and v:
                        for item in v:
                            if isinstance(item, str) and item.strip():
                                selected_images.append(item.strip())
                        break
        except Exception:
            selected_images = []
        selected_images = selected_images[:24]

        def progress_callback(progress, message):
            try:
                update_task(task_id, progress=int(progress or 0), message=message)
            except Exception:
                pass

        req = StoryShortsRequest(
            kind=kind,
            story_content=base_text,
            count=count,
            selected_images=selected_images or None,
            voice_style=voice_style,
            voice_gender=voice_gender,
        )
        result = _generate_story_shorts_payload(req, progress_callback=progress_callback) or {}
        shorts = result.get("shorts") if isinstance(result, dict) else None
        shorts = shorts if isinstance(shorts, list) else []

        created_ids: List[int] = []
        now = datetime.now()
        theme = f"Shorts: {(scheduled.title or 'Vídeo').strip()}"[:120]
        for idx, s in enumerate(shorts):
            if not isinstance(s, dict):
                continue
            video_url = (s.get("video_url") or "").strip()
            if not video_url:
                continue
            title = (s.get("title") or "").strip() or f"{(scheduled.title or 'Vídeo').strip()} (Short {idx+1})"
            description = (s.get("description") or "").strip()
            scheduled_for = now + timedelta(minutes=idx + 1)
            short_payload = {
                "source": "derived_short",
                "source_scheduled_video_id": scheduled.id,
                "parent_video_id": scheduled.id,
                "kind": kind,
                "video_type": "short",
                "title": title,
                "description": description,
                "video_url": video_url,
                "auto_processing_eligible": False,
                "processing_mode": "publish_only",
            }
            item = ScheduledVideo(
                theme=theme,
                title=title,
                description=description,
                scheduled_for=scheduled_for,
                status="completed",
                video_type="short",
                parent_video_id=scheduled.id,
                script_data=json.dumps(short_payload),
                auto_post=False,
                voice_style=getattr(scheduled, "voice_style", "human"),
                voice_gender=getattr(scheduled, "voice_gender", "female"),
            )
            try:
                setattr(item, "progress", 100)
            except Exception:
                pass
            try:
                setattr(item, "video_url", video_url)
            except Exception:
                pass
            db.add(item)
            db.flush()
            if item.id:
                created_ids.append(int(item.id))

        db.commit()
        update_task(
            task_id,
            progress=100,
            status="completed",
            message="Shorts criados e enviados para Aguardando Publicação.",
            result={"created_ids": created_ids, "count": len(created_ids)},
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")
    finally:
        try:
            db.close()
        except Exception:
            pass

@router.post("/schedule/from_generated")
def schedule_from_generated(request: QueueGeneratedVideoRequest, db: Session = Depends(get_db)):
    """Envia um vídeo já gerado para a fila 'Aguardando Publicação'.

    IDEMPOTENTE: se o vídeo (mesmo video_url + source generated_story) já existe em
    scheduled_videos, retorna o registro existente em vez de criar duplicado.
    Também reutiliza se houver mesma tarefa de origem (task_id) ou mesmo caminho
    de vídeo físico (video_path dentro de script_data JSON).
    """
    video_url = (request.video_url or "").strip()
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url é obrigatório.")

    kind = (request.kind or "").strip().lower()
    if kind not in {"story", "devotional", "prayer"}:
        kind = "story"

    video_type = (request.video_type or "").strip().lower() or "video"
    if video_type not in {"video", "short"}:
        video_type = "video"

    title = (request.title or "").strip() or f"Vídeo {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    description = (request.description or "").strip()

    scheduled_for = datetime.now()
    if request.scheduled_for:
        raw = str(request.scheduled_for).strip()
        try:
            scheduled_for = datetime.fromisoformat(raw)
        except Exception:
            try:
                scheduled_for = datetime.strptime(raw, "%Y-%m-%d %H:%M")
            except Exception:
                scheduled_for = datetime.now()

    video_path = (getattr(request, "video_path", None) or "").strip() or None
    task_id_src = (getattr(request, "task_id", None) or "").strip() or None
    scheduled_video_id = int(getattr(request, "scheduled_video_id", 0) or 0) or None
    unified_id = int(getattr(request, "unified_video_id", 0) or 0) or None

    # Generated local media must pass the same fail-closed probe used by the
    # renderer and recovery watchdog. A large file is not evidence of a valid
    # MP4, and ffprobe unavailability must never enqueue unverified media.
    local_media_candidate = video_path
    if not local_media_candidate and video_url and not video_url.startswith(("http://", "https://")):
        local_media_candidate = video_url
    if local_media_candidate:
        try:
            if os.path.isabs(local_media_candidate) and os.path.isfile(local_media_candidate):
                absolute_media_candidate = local_media_candidate
            else:
                from app.config import absolute_path_for_video as _absolute_path_for_generated_video

                absolute_media_candidate = _absolute_path_for_generated_video(os.path.basename(local_media_candidate))
        except Exception:
            absolute_media_candidate = ""
        media_probe = probe_media_file(absolute_media_candidate)
        if not media_probe.get("ok") or not media_durations_match(media_probe):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "generated_video_validation_failed",
                    "message": "O vídeo gerado não passou na validação real de áudio, vídeo e duração.",
                    "probe_error": media_probe.get("error"),
                    "checks": {
                        "file_exists": bool(media_probe.get("file_exists")),
                        "ffprobe_available": bool(media_probe.get("probe_available")),
                        "video_stream": bool(media_probe.get("video_stream")),
                        "audio_stream": bool(media_probe.get("audio_stream")),
                        "duration_match": media_durations_match(media_probe),
                    },
                },
            )

    # === IDEMPOTÊNCIA: BUSCA EXISTENTE ===
    # Estratégias (qualquer uma que bater → reutiliza):
    #   (a) scheduled_video_id explícito no request.
    #   (b) task_id correspondente dentro de script_data OU task_id coluna nova se houver.
    #   (c) video_url idêntico + source=generated_story.
    #   (d) video_path idêntico dentro de script_data.
    existing: Optional[ScheduledVideo] = None
    try:
        if scheduled_video_id:
            existing = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(scheduled_video_id)).first()
        if existing is None and unified_id:
            existing = (
                db.query(ScheduledVideo)
                .filter(ScheduledVideo.unified_video_id == int(unified_id))
                .order_by(ScheduledVideo.id.desc())
                .first()
            )
        if existing is None and task_id_src:
            existing = (
                db.query(ScheduledVideo)
                .filter(ScheduledVideo.task_id == str(task_id_src))
                .order_by(ScheduledVideo.id.desc())
                .first()
            )
        if existing is None and task_id_src:
            try:
                rows = db.query(ScheduledVideo).order_by(ScheduledVideo.id.desc()).limit(500).all()
                for r in rows:
                    try:
                        sd = json.loads(getattr(r, "script_data", "") or "{}") if getattr(r, "script_data", None) else {}
                        if isinstance(sd, dict) and str(sd.get("task_id") or "") == str(task_id_src):
                            existing = r
                            break
                    except Exception:
                        continue
            except Exception:
                existing = None
        if existing is None:
            rows = db.query(ScheduledVideo).order_by(ScheduledVideo.id.desc()).limit(1000).all()
            for r in rows:
                try:
                    sd = json.loads(getattr(r, "script_data", "") or "{}") if getattr(r, "script_data", None) else {}
                    if not isinstance(sd, dict):
                        continue
                    if sd.get("source") == "generated_story":
                        r_url = str(getattr(r, "video_url") or sd.get("video_url") or "").strip()
                        r_path = str(sd.get("video_path") or "").strip()
                        if r_url and video_url and r_url == video_url:
                            existing = r
                            break
                        if r_path and video_path and r_path == video_path:
                            existing = r
                            break
                except Exception:
                    continue
    except Exception:
        existing = None

    if existing is not None:
        try:
            existing_db = existing
            dirty = False
            if getattr(existing_db, "progress", None) in (None, 0):
                try:
                    setattr(existing_db, "progress", 100)
                    dirty = True
                except Exception:
                    pass
            if not getattr(existing_db, "status", None) or str(getattr(existing_db, "status") or "").lower() not in {"completed", "published", "failed"}:
                existing_db.status = "completed"
                dirty = True
            if request.auto_post and not getattr(existing_db, "auto_post", False):
                existing_db.auto_post = True
                dirty = True
            if not getattr(existing_db, "video_url", None) and video_url:
                try:
                    setattr(existing_db, "video_url", video_url)
                    dirty = True
                except Exception:
                    pass
            if task_id_src and getattr(existing_db, "task_id", None) != task_id_src:
                existing_db.task_id = task_id_src
                dirty = True
            if unified_id and getattr(existing_db, "unified_video_id", None) != unified_id:
                existing_db.unified_video_id = unified_id
                dirty = True
            if video_path and getattr(existing_db, "video_path", None) != video_path:
                existing_db.video_path = video_path
                dirty = True
            if unified_id and getattr(existing_db, "pipeline", None) != "unified_video_pipeline":
                existing_db.pipeline = "unified_video_pipeline"
                dirty = True
            if request.title and title and title != getattr(existing_db, "title", None):
                existing_db.title = title
                dirty = True
            if request.description and description and description != getattr(existing_db, "description", None):
                existing_db.description = description
                dirty = True
            if dirty:
                db.commit()
                try:
                    db.refresh(existing_db)
                except Exception:
                    pass
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        return {
            "id": existing.id,
            "status": getattr(existing, "status", "completed"),
            "video_url": getattr(existing, "video_url", None) or video_url,
            "reused_existing": True,
            "idempotency": "ok: matched_existing_scheduled_video",
        }

    payload: Dict[str, Any] = {
        "source": "generated_story",
        "kind": kind,
        "video_type": video_type,
        "title": title,
        "description": description,
        "video_url": video_url,
        "auto_processing_eligible": False,
        "processing_mode": "publish_only",
    }
    if video_path:
        payload["video_path"] = video_path
    if task_id_src:
        payload["task_id"] = task_id_src
    if request.voice_style:
        payload["voice_style"] = request.voice_style
    if request.voice_gender:
        payload["voice_gender"] = request.voice_gender
    if unified_id:
        payload["unified_video_id"] = int(unified_id)

    video = ScheduledVideo(
        theme="História/Devocional",
        title=title,
        description=description,
        scheduled_for=scheduled_for,
        video_type=video_type,
        script_data=json.dumps(payload),
        status="completed",
        auto_post=bool(request.auto_post),
        task_id=task_id_src,
        unified_video_id=unified_id,
        video_path=video_path,
        pipeline=("unified_video_pipeline" if unified_id else None),
    )
    try:
        setattr(video, "progress", 100)
    except Exception:
        pass
    if request.voice_style:
        try:
            setattr(video, "voice_style", request.voice_style)
        except Exception:
            pass
    if request.voice_gender:
        try:
            setattr(video, "voice_gender", request.voice_gender)
        except Exception:
            pass
    try:
        setattr(video, "video_url", video_url)
    except Exception:
        pass

    db.add(video)
    db.commit()
    db.refresh(video)

    return {
        "id": video.id,
        "status": video.status,
        "video_url": video.video_url,
        "reused_existing": False,
        "idempotency": "ok: created_new_scheduled_video",
    }

@router.get("/reports")
def get_reports(db: Session = Depends(get_db)):
    """Retorna o histórico de relatórios de monitoramento"""
    return db.query(ChannelReport).order_by(ChannelReport.id.desc()).limit(20).all()

@router.get("/debug-auth")
def debug_auth(db: Session = Depends(get_db)):
    """Debug endpoint to check DB credentials state"""
    settings = db.query(Settings).first()
    service = YouTubeService()
    env_client_id = bool((os.getenv("YOUTUBE_CLIENT_ID") or "").strip())
    env_client_secret = bool((os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip())
    env_refresh = bool((os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip())
    return {
        "status": "Settings found" if settings else "No settings found",
        "db_has_client_id": bool(settings and settings.youtube_client_id),
        "db_has_client_secret": bool(settings and settings.youtube_client_secret),
        "db_has_refresh_token": bool(settings and settings.youtube_refresh_token),
        "env_has_client_id": env_client_id,
        "env_has_client_secret": env_client_secret,
        "env_has_refresh_token": env_refresh,
        "service_connected": bool(service.service),
        "service_auth_source": getattr(service, "auth_source", None),
        "service_auth_error": getattr(service, "auth_error", None),
    }

@router.get("/stats")
def get_stats():
    service = YouTubeService()
    return service.get_channel_stats()

@router.get("/videos")
def list_videos():
    """Lista os vídeos gerados na pasta videos"""
    # Corrigido para listar da pasta correta onde o VideoGenerator salva
    video_files = glob.glob("app/static/videos/*.mp4")
    videos = []
    for f in video_files:
        filename = os.path.basename(f)
        videos.append({
            "filename": filename,
            "url": f"/static/videos/{filename}",
            "created_at": os.path.getctime(f)
        })
    # Ordenar por data de criação (mais recente primeiro)
    videos.sort(key=lambda x: x['created_at'], reverse=True)
    return videos

@router.get("/auth_url")
def get_auth_url(
    redirect_uri: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Retorna JSON com auth_url, state e redirect_uri. Fluxo novo (não OOB)."""
    try:
        settings = db.query(Settings).first()
        has_db_creds = settings and (settings.youtube_client_id or "").strip() and (settings.youtube_client_secret or "").strip()
        has_env_creds = (os.getenv("YOUTUBE_CLIENT_ID") or "").strip() and (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
        has_file = os.path.exists("client_secret.json")
        if not has_db_creds and not has_env_creds and not has_file:
            raise HTTPException(
                status_code=503,
                detail="Configure as credenciais do YouTube em Configurações (Client ID e Client Secret), ou nas variáveis YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET, ou use client_secret.json no servidor."
            )
        service = YouTubeService()
        result = service.get_auth_url_with_state(redirect_uri=redirect_uri)
        auth_url = result.get("auth_url")
        state = result.get("state")
        final_redirect_uri = result.get("redirect_uri")
        if not auth_url or not state:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a URL de autorização. Verifique se Client ID e Client Secret em Configurações estão corretos (Google Cloud Console)."
            )
        return {
            "auth_url": auth_url,
            "state": state,
            "redirect_uri": final_redirect_uri,
            "note": "Fluxo novo: abra auth_url no navegador, autorize. O callback /youtube/auth/callback troca o código automaticamente. Não copie códigos manualmente.",
        }
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="Arquivo client_secret.json não encontrado. Configure Client ID e Client Secret em Configurações (Google Cloud Console > APIs & Services > Credentials)."
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Erro ao conectar ao YouTube: {str(e)}"
        )


@router.get("/auth/callback")
def auth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Callback OAuth moderno Google. Redireciona de volta ao front com status via query."""
    import urllib.parse as _up
    from fastapi.responses import RedirectResponse

    safe_audit: Dict[str, Any] = {
        "http_status": None,
        "error": error or None,
        "error_description": error_description or None,
        "redirect_uri": None,
        "refresh_token_present": False,
        "state_valid": False,
        "state_consumed": False,
        "pkce_preserved": False,
        "redirect_uri_consistent": False,
        "persisted": False,
        "service_verified": False,
    }
    message_human = ""
    status_label = "error"
    try:
        if error:
            safe_audit["error"] = str(error)
            safe_audit["error_description"] = str(error_description or "")
            message_human = f"Google rejeitou a autorização (error={error}). {str(error_description or '')}"
        else:
            service = YouTubeService()
            ok, msg, audit = service.exchange_code_for_token_with_state(
                code=code or "",
                state=state or "",
            )
            for k in list(safe_audit.keys()):
                if k in audit and audit[k] is not None:
                    safe_audit[k] = audit[k]
            message_human = msg
            if ok:
                status_label = "success"
            else:
                status_label = "fail"
    except Exception as e:
        status_label = "error"
        safe_audit["error"] = safe_audit["error"] or "callback_exception"
        safe_audit["error_description"] = safe_audit["error_description"] or f"{type(e).__name__}: {str(e)[:300]}"
        message_human = safe_audit["error_description"] or ""
    try:
        from app.services.youtube_service import logger as _yt_logger
        _yt_logger.warning(
            "YouTube OAuth /auth/callback final: status=%s err=%s http=%s rt=%s state_ok=%s pkce=%s rt_consistent=%s persist=%s verify=%s refresh=%s",
            status_label,
            (safe_audit.get("error") or "")[:60],
            str(safe_audit.get("http_status") or ""),
            (safe_audit.get("redirect_uri") or "")[:80],
            bool(safe_audit.get("state_valid")),
            bool(safe_audit.get("pkce_preserved")),
            bool(safe_audit.get("redirect_uri_consistent")),
            bool(safe_audit.get("persisted")),
            bool(safe_audit.get("service_verified")),
            bool(safe_audit.get("refresh_token_present")),
        )
    except Exception:
        pass
    redirect_base = "/youtube-auto?tab=settings"
    try:
        q = _up.urlencode({
            "oauth": status_label,
            "msg": (message_human or "")[:500],
            "err": (safe_audit.get("error") or "")[:120],
        })
        url = f"{redirect_base}#{q}"
        return RedirectResponse(url=url)
    except Exception:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            content=f"<html><body><p>Status={status_label}</p><p>{message_human}</p><p>err={safe_audit.get('error')}</p><script>setTimeout(()=>location.href='{redirect_base}', 2000);</script></body></html>",
            status_code=200,
        )


@router.post("/auth/exchange")
def exchange_code(data: Dict[str, str]):
    code = data.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Código não fornecido")

    original_code = code
    code = str(code).strip().replace(" ", "").replace("\n", "").replace("\r", "")
    print(f"Exchange code: original length {len(original_code)}, sanitized length {len(code)}")

    service = YouTubeService()
    success, message = service.exchange_code_for_token(code)

    if success:
        return {"message": message}
    else:
        print(f"Erro detalhado na troca de código: {message}")
        raise HTTPException(
            status_code=400,
            detail=f"Falha ao autenticar: {message}\n\n"
                   "Fluxo moderno recomendado (OOB descontinuado pelo Google em 2022+):\n"
                   "1. Clique em Conectar YouTube.\n"
                   "2. Autorize no Google.\n"
                   "3. Deixe o callback automático /youtube/auth/callback processar.\n"
                   "4. Não copie códigos manualmente.\n"
                   "Google Cloud: tipo Desktop ou Web com redirect_uri "
                   "http://127.0.0.1:8010/youtube/auth/callback",
        )


@router.post("/optimize")
def optimize_channel(execute: bool = False):
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    analysis = yt_service.optimize_channel(ai_service)
    
    if execute and analysis:
        # Map analysis result to execution format
        # analysis expected to have: title, description, strategy (for banner prompt)
        exec_data = {
            "title": analysis.get("title_suggestion"),
            "description": analysis.get("description_suggestion"),
            "banner_prompt": analysis.get("banner_prompt")
        }
        
        # Execute immediately
        execution_results = execute_optimization(exec_data)
        
        # Merge results
        analysis["execution_results"] = execution_results
        
    return analysis

@router.post("/auto-analysis")
def auto_analysis():
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    # 1. Fetch Stats
    stats = yt_service.get_channel_stats()
    # Optimized: Limit to 5 videos to speed up AI analysis (was 10)
    recent_videos = yt_service.get_recent_videos_stats(limit=5)
    
    if not stats.get("connected"):
        raise HTTPException(status_code=400, detail="Canal não conectado. Por favor, conecte-se na aba Configurações.")
    
    # 2. Analyze with AI using centralized service
    return ai_service.generate_auto_insights(stats, recent_videos)

@router.post("/monetization-status")
def monetization_status():
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    stats = yt_service.get_channel_stats()
    
    if not stats.get("connected"):
        raise HTTPException(status_code=400, detail="Canal não conectado.")

    # Estimate Watch Hours (very rough assumption: 3 mins per view average)
    total_views = int(stats.get('views', 0))
    estimated_minutes = total_views * 3
    estimated_hours = int(estimated_minutes / 60)
    
    subscribers = int(stats.get('subscribers', 0))
    
    # Prepare data for AI service
    progress_data = {
        "subscribers": subscribers,
        "subscribers_target": 1000,
        "estimated_watch_hours": estimated_hours,
        "watch_hours_target": 4000,
        "subscribers_progress_pct": round((subscribers / 1000) * 100, 1),
        "watch_hours_progress_pct": round((estimated_hours / 4000) * 100, 1)
    }
    
    # Analyze with AI
    ai_result = ai_service.generate_monetization_insights(progress_data)
    
    # Structure for Frontend
    final_response = {
        "ai_insights": ai_result,
        "progress": {
            "subscribers": subscribers,
            "subscribers_progress_pct": progress_data["subscribers_progress_pct"],
            "estimated_watch_hours": estimated_hours,
            "watch_hours_progress_pct": progress_data["watch_hours_progress_pct"]
        }
    }
    
    return final_response

@router.post("/optimize/execute")
def execute_optimization(data: Dict[str, Any]):
    """Executa as melhorias sugeridas (título/descrição/banner)"""
    from app.services.ai_generator import AIContentGenerator
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    # data expects {'title': '...', 'description': '...', 'banner_prompt': '...'}
    
    results = {
        "banner_generated": False,
        "banner_uploaded": False,
        "channel_updated": False,
        "errors": []
    }

    banner_url = None
    if data.get('banner_prompt'):
        # 1. Generate Image
        try:
            generated_image_url = ai_service.generate_banner_image(data['banner_prompt'])
            if generated_image_url:
                results["banner_generated"] = True
                # 2. Upload to YouTube
                # Convert relative path to absolute (compatível com Docker)
                from app.config import absolute_path_for_static
                banner_path = generated_image_url
                if banner_path.startswith("/"):
                    banner_path = absolute_path_for_static(banner_path)
                
                banner_url = yt_service.upload_channel_banner(banner_path)
                if banner_url:
                    results["banner_uploaded"] = True
                else:
                    results["errors"].append("Falha ao fazer upload do banner para o YouTube")
            else:
                results["errors"].append("Falha ao gerar imagem do banner com IA")
        except Exception as e:
            results["errors"].append(f"Erro no processamento do banner: {str(e)}")
    
    # 3. Update Channel Info
    update_res = yt_service.update_channel_info(
        title=data.get('title'), 
        description=data.get('description'),
        banner_external_url=banner_url
    )
    
    if "error" in update_res:
        results["errors"].append(f"Erro ao atualizar canal: {update_res['error']}")
    else:
        results["channel_updated"] = True
        results["update_details"] = update_res
        
    return results

class ScheduleRequest(BaseModel):
    theme: str
    duration_type: str = "days" # days, weeks, months
    duration_value: int = 7
    start_date: Optional[str] = None # YYYY-MM-DD
    videos_per_day: int = 1
    shorts_per_day: int = 0
    video_duration: int = 5

    script_data: Optional[str] = None
    music_file_path: Optional[str] = None # Path to uploaded music file
    music_mode: bool = False

@router.put("/schedule/{video_id}")
def update_scheduled_video(video_id: int, data: Dict[str, Any], db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    if "scheduled_for" in data:
        try:
            # Expects ISO format or "YYYY-MM-DD HH:MM"
            dt_str = data["scheduled_for"]
            if "T" in dt_str:
                video.scheduled_for = datetime.fromisoformat(dt_str)
            else:
                video.scheduled_for = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            pass # Keep old value if format error
            
    if "auto_post" in data:
        video.auto_post = bool(data["auto_post"])
        
    if "title" in data:
        video.title = data["title"]

    if "voice_style" in data:
        video.voice_style = data["voice_style"]
        
    if "voice_gender" in data:
        video.voice_gender = data["voice_gender"]
        
    db.commit()
    return {"message": "Video updated", "video": {
        "id": video.id, 
        "scheduled_for": video.scheduled_for.isoformat() if video.scheduled_for else None,
        "auto_post": video.auto_post
    }}

@router.post("/schedule/generate")
def generate_schedule(request: ScheduleRequest):
    from app.services.ai_generator import AIContentGenerator
    ai_service = AIContentGenerator()
    try:
        return ai_service.generate_content_plan(
            request.theme, 
            request.duration_type, 
            request.duration_value, 
            request.start_date,
            request.videos_per_day,
            request.shorts_per_day,
            request.video_duration
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from sqlalchemy import text, inspect

@router.post("/schedule/save")
def save_schedule(plan: List[Dict[str, Any]], background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Salva o plano no banco de dados e inicia geração"""
    
    # Auto-fix: Ensure columns exist (fail-safe for migration issues)
    # List of potentially missing columns and their types
    # Added comprehensive check for all new columns
    missing_cols = [
        ("progress", "INTEGER DEFAULT 0"),
        ("publish_at", "DATETIME"),
        ("auto_post", "BOOLEAN DEFAULT 0"),
        ("voice_style", "VARCHAR"),
        ("voice_gender", "VARCHAR"),
        ("music_file_path", "VARCHAR"),
        ("youtube_video_id", "VARCHAR"),
        ("uploaded_at", "DATETIME"),
        ("updated_at", "DATETIME")
    ]
    
    # Compatibilidade defensiva para schemas legados antes da migração Alembic formal
    try:
        inspector = inspect(db.get_bind())
        columns = [c["name"] for c in inspector.get_columns("scheduled_videos")]
        
        for col_name, col_type in missing_cols:
            if col_name not in columns:
                try:
                    db.execute(text(f"ALTER TABLE scheduled_videos ADD COLUMN {col_name} {col_type}"))
                    db.commit()
                    print(f"Migration: Added column {col_name} to scheduled_videos")
                except Exception as e:
                    print(f"Migration error ({col_name}): {e}")
                    db.rollback()
    except Exception as e:
        print(f"Migration check failed: {e}")

    saved_videos = []
    created_videos = []
    skipped_duplicates = []
    
    for item in plan:
        # Extrair dados do item
        # Se for music_mode, o item já deve vir com music_file_path
        schedule_payload = item.get("videos", [{}])[0] if isinstance(item.get("videos"), list) else dict(item)
        if not isinstance(schedule_payload, dict):
            schedule_payload = {}
        else:
            schedule_payload = dict(schedule_payload)
        schedule_payload["source"] = "manual_schedule"
        schedule_payload["auto_processing_eligible"] = True
        schedule_payload["processing_mode"] = "scheduled_automation"
        scheduled_for = datetime.strptime(
            f"{item.get('date')} {item.get('videos', [{}])[0].get('time', '12:00')}",
            "%Y-%m-%d %H:%M",
        ) if item.get("date") else datetime.now().replace(second=0, microsecond=0)
        theme = item.get("theme_of_day", "Geral")
        video_type = item.get("videos", [{}])[0].get("type", "video") if isinstance(item.get("videos"), list) else item.get("type", "video")
        equivalence_key = _build_scheduled_video_equivalence_key(
            user_id=None,
            theme=theme,
            scheduled_for=scheduled_for,
            video_type=video_type,
        )
        _acquire_scheduled_video_creation_lock(db, equivalence_key)
        existing_video = _find_active_equivalent_scheduled_video(
            db,
            user_id=None,
            theme=theme,
            scheduled_for=scheduled_for,
            video_type=video_type,
        )
        if existing_video is not None:
            skipped_duplicates.append({
                "existing_id": existing_video.id,
                "theme": existing_video.theme,
                "scheduled_for": scheduled_for.isoformat(),
                "video_type": existing_video.video_type,
                "status": existing_video.status,
            })
            continue
        
        video = ScheduledVideo(
            theme=theme,
            title=item.get("videos", [{}])[0].get("title", "Vídeo Agendado") if isinstance(item.get("videos"), list) else item.get("title", "Vídeo"),
            description=item.get("videos", [{}])[0].get("concept", "") if isinstance(item.get("videos"), list) else item.get("concept", ""),
            scheduled_for=scheduled_for,
            video_type=video_type,
            script_data=json.dumps(schedule_payload),
            status="queued", # Start as queued
            auto_post=item.get("videos", [{}])[0].get("auto_post", True) if isinstance(item.get("videos"), list) else item.get("auto_post", True),
            voice_style=item.get("videos", [{}])[0].get("voice_style", "human") if isinstance(item.get("videos"), list) else item.get("voice_style", "human"),
            voice_gender=item.get("videos", [{}])[0].get("voice_gender", "female") if isinstance(item.get("videos"), list) else item.get("voice_gender", "female"),
            music_file_path=item.get("videos", [{}])[0].get("music_file_path") if isinstance(item.get("videos"), list) else item.get("music_file_path")
        )
        db.add(video)
        db.flush() # get ID
        saved_videos.append(video)
        created_videos.append(video)
    
    db.commit()
    
    # Kickoff imediato do primeiro item para não depender exclusivamente do scheduler
    # (evita sensação de "não está gerando").
    if created_videos:
        try:
            processing = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").first()
            if not processing:
                from app.services.video_processing import process_scheduled_video
                background_tasks.add_task(process_scheduled_video, created_videos[0].id)
        except Exception as e:
            print(f"Erro ao iniciar geração imediata: {e}")
    
    return {
        "message": "Schedule saved",
        "count": len(created_videos),
        "deduplicated_count": len(skipped_duplicates),
        "duplicates": skipped_duplicates[:20],
    }

def _queue_manual_scheduled_generation(
    video: ScheduledVideo,
    background_tasks: BackgroundTasks,
    db: Session,
    *,
    manual_action: str,
) -> Dict[str, Any]:
    payload = _load_scheduled_video_payload(video)
    policy = _scheduled_video_processing_policy(video, payload)
    source_label = str(policy.get("source") or payload.get("source") or "legacy_schedule").strip() or "legacy_schedule"
    normalized_status = str(video.status or "").strip().lower()
    blocked_for_manual = source_label in _SCHEDULED_AUTO_BLOCKED_SOURCES or bool(
        str(payload.get("source_production_video_id") or "").strip()
    )

    if blocked_for_manual:
        raise HTTPException(
            status_code=409,
            detail=f"Este item ({source_label}) é somente para publicação/manual queue e não pode ser regenerado por esta rota.",
        )

    if normalized_status == "processing":
        raise HTTPException(
            status_code=409,
            detail="Este vídeo já está em processamento. Aguarde a conclusão para evitar geração paralela.",
        )

    if normalized_status == "queued":
        raise HTTPException(
            status_code=409,
            detail="Este vídeo já está enfileirado. Não é permitido iniciar uma segunda geração paralela.",
        )

    if normalized_status == "published" or getattr(video, "uploaded_at", None):
        raise HTTPException(
            status_code=409,
            detail="Este vídeo já foi publicado. Crie uma nova solicitação se precisar de outra versão.",
        )

    payload["source"] = payload.get("source") or "manual_schedule"
    payload["auto_processing_eligible"] = True
    payload["processing_mode"] = "manual_generation"
    payload["manual_generation_requested"] = True
    payload["manual_generation_action"] = manual_action
    payload["manual_generation_requested_at"] = datetime.now().isoformat()

    # Limpar cache de script se existir, para forçar regeneração da IA (pois o usuário pediu explicitamente)
    changed = False
    if payload:
        keys_to_remove = ["scenes", "audio_path", "background_music", "music_credit", "render_report"]
        for k in keys_to_remove:
            if k in payload:
                del payload[k]
                changed = True
    if changed or not video.script_data:
        video.script_data = json.dumps(payload)

    # IMPORTANTE: força regeneração real.
    # Sem limpar video_url, o processador interpreta como "já pronto" e só recupera status.
    old_video_url = (video.video_url or "").strip()
    if old_video_url:
        try:
            from app.config import absolute_path_for_video
            old_abs_path = absolute_path_for_video(old_video_url)
            if old_abs_path and os.path.exists(old_abs_path):
                os.remove(old_abs_path)
        except Exception as e:
            print(f"Erro ao remover vídeo antigo ({old_video_url}): {e}")

    # Limpar metadados de publicação/arquivo para que "Regerar" não reutilize artefatos antigos
    video.video_url = None
    video.youtube_video_id = None
    video.uploaded_at = None

    # Limpa marcadores de erro sistêmico antigos para não poluir UI após retry
    if video.description:
        markers = ("[ERRO]", "[SISTEMA]", "[UPLOAD_ERRO]", "[AUTO_BLOCKED]")
        cleaned_lines = [ln for ln in video.description.splitlines() if not any(m in ln for m in markers)]
        video.description = "\n".join(cleaned_lines).strip()

    video.status = "queued"
    video.progress = 0
    db.commit()
    
    # Dispara tentativa imediata quando a fila está livre.
    # Se já houver um item processando, o scheduler assume o próximo ciclo.
    try:
        processing = db.query(ScheduledVideo).filter(
            ScheduledVideo.status == "processing",
            ScheduledVideo.id != video.id
        ).first()
        if not processing:
            from app.services.video_processing import process_scheduled_video
            background_tasks.add_task(process_scheduled_video, video.id, "manual")
    except Exception as e:
        print(f"Erro ao iniciar geração manual explícita do vídeo {video.id}: {e}")

    return {
        "status": "queued",
        "id": video.id,
        "source": policy.get("source"),
        "manual_action": manual_action,
    }

@router.post("/schedule/{video_id}/generate")
def generate_scheduled_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return _queue_manual_scheduled_generation(
        video,
        background_tasks,
        db,
        manual_action="generate",
    )

@router.post("/schedule/{video_id}/regenerate")
def regenerate_scheduled_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Mesma coisa que generate, mas semanticamente explícito"""
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return _queue_manual_scheduled_generation(
        video,
        background_tasks,
        db,
        manual_action="regenerate",
    )

@router.post("/schedule/{video_id}/publish-now")
def publish_now_scheduled_video(video_id: int, db: Session = Depends(get_db)):
    """Publica imediatamente um vídeo que está em Aguardando Publicação (status completed)."""
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado.")
    normalized_status = (video.status or "").lower().strip()
    if normalized_status not in ("completed", "ready"):
        raise HTTPException(status_code=400, detail="Só é possível publicar vídeos prontos (status concluído).")
    if video.uploaded_at:
        raise HTTPException(status_code=400, detail="Este vídeo já foi publicado.")
    if not video.video_url:
        raise HTTPException(status_code=400, detail="Vídeo sem arquivo. Regenere o vídeo.")

    abs_video_path = _resolve_video_file_path(video.video_url)
    if not abs_video_path or not os.path.exists(abs_video_path):
        raise HTTPException(
            status_code=503,
            detail="Arquivo de vídeo não encontrado no servidor. Exclua este item ou agende um novo."
        )

    tags = ["motivação", "sucesso"]
    if video.script_data:
        try:
            script = json.loads(video.script_data)
            if script.get("tags"):
                tags = script["tags"]
        except Exception:
            pass

    yt_service = YouTubeService()
    upload_result = yt_service.upload_video(
        abs_video_path,
        title=video.title,
        description=video.description or "Vídeo gerado automaticamente por Codexia.",
        tags=tags,
    )

    video_id_value = _extract_uploaded_youtube_id(upload_result)

    if not video_id_value:
        # Mantém vídeo pronto para nova tentativa manual após configurar credenciais.
        if normalized_status in ("ready", "completed"):
            video.status = normalized_status
        err_msg = _publish_error_message(upload_result, action_label="publicar")
        video.description = _append_upload_error_note(video.description, err_msg)
        db.commit()
        raise HTTPException(status_code=502, detail=err_msg)

    video.uploaded_at = datetime.now()
    video.youtube_video_id = video_id_value
    video.status = "published"
    db.commit()
    return {
        "status": "published",
        "youtube_video_id": video_id_value,
        "youtube_url": _build_youtube_watch_url(video_id_value),
        "message": "Vídeo publicado com sucesso!",
    }

@router.post("/schedule/{video_id}/republish")
def republish_scheduled_video(video_id: int, db: Session = Depends(get_db)):
    """Republica no YouTube um vídeo já publicado (re-envia o mesmo arquivo; gera novo ID no YouTube)."""
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado.")
    normalized_status = (video.status or "").lower().strip()
    if normalized_status not in ("completed", "ready", "published"):
        raise HTTPException(status_code=400, detail="Só é possível republicar vídeos já produzidos ou publicados.")
    if not video.video_url:
        raise HTTPException(status_code=400, detail="Vídeo sem arquivo. Regenere o vídeo.")

    abs_video_path = _resolve_video_file_path(video.video_url)
    if not abs_video_path or not os.path.exists(abs_video_path):
        raise HTTPException(
            status_code=503,
            detail="Arquivo de vídeo não encontrado no servidor. Não é possível republicar."
        )

    tags = ["motivação", "sucesso"]
    if video.script_data:
        try:
            script = json.loads(video.script_data)
            if script.get("tags"):
                tags = script["tags"]
        except Exception:
            pass

    yt_service = YouTubeService()
    upload_result = yt_service.upload_video(
        abs_video_path,
        title=video.title,
        description=video.description or "Vídeo gerado automaticamente por Codexia.",
        tags=tags,
    )

    video_id_value = _extract_uploaded_youtube_id(upload_result)

    if not video_id_value:
        err_msg = _publish_error_message(upload_result, action_label="republicar")
        video.description = _append_upload_error_note(video.description, err_msg)
        db.commit()
        raise HTTPException(status_code=502, detail=err_msg)

    video.uploaded_at = datetime.now()
    video.youtube_video_id = video_id_value
    video.status = "published"
    db.commit()
    return {
        "status": "published",
        "youtube_video_id": video_id_value,
        "youtube_url": _build_youtube_watch_url(video_id_value),
        "message": "Vídeo republicado com sucesso!",
    }

@router.delete("/schedule/{video_id}")
def delete_scheduled_video(video_id: int, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Opcional: deletar arquivo físico se existir
    if video.video_url:
        try:
            abs_path = _resolve_video_file_path(video.video_url)
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except Exception as e:
            print(f"Erro ao deletar arquivo: {e}")

    db.delete(video)
    db.commit()
    return {"status": "deleted"}

@router.get("/schedule/{video_id}/download")
def download_scheduled_video(video_id: int, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Download do arquivo de vídeo de um item agendado."""
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    path = _resolve_video_file_path(video.video_url)
    if not path:
        raise HTTPException(status_code=404, detail="Video file not found")

    return FileResponse(path, media_type="video/mp4", filename=os.path.basename(path))

@router.get("/schedule")
def get_schedule(
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    video_type: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Lista vídeos agendados; inclui description e error_msg para exibir erro na UI (Ver Erro)."""
    from sqlalchemy import func, or_

    _sync_ready_unified_to_scheduled(db)
    _sync_ready_production_to_scheduled(db)

    base_q = db.query(ScheduledVideo)

    status_norm = (status or "").strip().lower()
    if status_norm:
        col = func.lower(func.trim(func.coalesce(ScheduledVideo.status, "")))
        if status_norm in {"ready", "completed"}:
            base_q = base_q.filter(col.in_(["ready", "completed"]))
        elif status_norm == "published":
            base_q = base_q.filter(col.like("%published%"))
        elif status_norm in {"failed", "error"}:
            base_q = base_q.filter(col.in_(["failed", "error"]))

    vt_norm = (video_type or "").strip().lower()
    if vt_norm and vt_norm in {"video", "short"}:
        base_q = base_q.filter(func.lower(func.trim(func.coalesce(ScheduledVideo.video_type, ""))) == vt_norm)

    q_norm = (q or "").strip()
    if q_norm:
        ql = q_norm.lower()
        like_term = f"%{ql}%"
        filters = [
            func.lower(func.coalesce(ScheduledVideo.title, "")).like(like_term),
            func.lower(func.coalesce(ScheduledVideo.theme, "")).like(like_term),
            func.lower(func.coalesce(ScheduledVideo.description, "")).like(like_term),
            func.lower(func.coalesce(ScheduledVideo.youtube_video_id, "")).like(like_term),
        ]
        if ql.isdigit():
            try:
                filters.append(ScheduledVideo.id == int(ql))
            except Exception:
                pass
        base_q = base_q.filter(or_(*filters))

    total = int(base_q.count() or 0)
    rows = (
        base_q.order_by(ScheduledVideo.id.desc())
        .offset(int(offset or 0))
        .limit(int(limit or 200))
        .all()
    )

    result = []
    for v in rows:
        desc = v.description or ""
        err = ""
        if "[ERRO]" in desc:
            idx = desc.find("[ERRO]")
            err = desc[idx:].replace("[ERRO]:", "").strip()[:2000]
        result.append({
            "id": v.id,
            "theme": v.theme,
            "title": v.title,
            "description": desc,
            "error_msg": err or (desc if (v.status or "").lower() == "failed" else ""),
            "status": v.status,
            "progress": v.progress or 0,
            "scheduled_for": v.scheduled_for.isoformat() if v.scheduled_for else None,
            "auto_post": getattr(v, "auto_post", False),
            "video_type": v.video_type,
            "parent_video_id": v.parent_video_id,
            "video_url": _normalize_video_url_for_client(v.video_url),
            "youtube_video_id": v.youtube_video_id,
            "uploaded_at": v.uploaded_at.isoformat() if getattr(v, "uploaded_at", None) else None,
            "voice_style": getattr(v, "voice_style", "human"),
            "voice_gender": getattr(v, "voice_gender", "female"),
            "task_id": getattr(v, "task_id", None),
            "unified_video_id": getattr(v, "unified_video_id", None),
            "video_path": getattr(v, "video_path", None),
            "pipeline": getattr(v, "pipeline", None),
        })

    off = int(offset or 0)
    lim = int(limit or 200)
    has_more = (off + len(result)) < total
    return {"items": result, "total": total, "limit": lim, "offset": off, "has_more": has_more}

@router.get("/auto_insights")
def get_auto_insights():
    """
    Auto Análise:
    - Lê estatísticas gerais do canal
    - Lê performance recente dos vídeos
    - Pede para a IA gerar resumo + novas ideias de vídeos/shorts
    """
    yt = YouTubeService()
    ai = AIContentGenerator()

    stats = yt.get_channel_stats()
    videos = yt.get_recent_videos_performance(max_results=20)
    ai_insights = ai.generate_auto_insights(stats, videos)

    return {
        "stats": stats,
        "recent_videos": videos,
        "ai_insights": ai_insights,
    }

@router.get("/topic_suggestions")
def get_topic_suggestions(
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin_user),
):
    latest = (
        db.query(ChannelInsight)
        .filter(ChannelInsight.kind == "topic_suggestions")
        .order_by(ChannelInsight.id.desc())
        .first()
    )
    if latest and latest.data_json and (not refresh):
        try:
            data = json.loads(latest.data_json)
        except Exception:
            data = {"summary": latest.ai_summary or "Sugestões disponíveis.", "raw": latest.data_json}
        return {
            "generated_at": (latest.created_at.isoformat() if getattr(latest, "created_at", None) else None),
            "summary": latest.ai_summary,
            "data": data,
        }

    yt = YouTubeService()
    ai = AIContentGenerator()
    stats = yt.get_channel_stats()
    videos = yt.get_recent_videos_performance(max_results=20)
    comments_q = (
        db.query(CommunityComment)
        .order_by(CommunityComment.published_at.desc().nullslast(), CommunityComment.created_at.desc().nullslast())
        .limit(80)
        .all()
    )
    comments = []
    for c in comments_q or []:
        t = (c.text or "").strip()
        if not t:
            continue
        comments.append({
            "text": t[:500],
            "like_count": int(getattr(c, "like_count", 0) or 0),
            "published_at": (c.published_at.isoformat() if getattr(c, "published_at", None) else None),
            "video_id": (c.youtube_video_id or None),
        })
    data = ai.generate_topic_suggestions(stats=stats, recent_videos=videos, recent_comments=comments, hours=72) or {}
    summary = (data.get("summary") if isinstance(data, dict) else None) or "Sugestões de temas atualizadas."
    db.add(ChannelInsight(
        user_id=None,
        kind="topic_suggestions",
        start_date=None,
        end_date=None,
        data_json=json.dumps(data, ensure_ascii=False),
        ai_summary=str(summary)[:1200],
    ))
    try:
        top_titles = []
        for idea in (data.get("long_video_ideas") or [])[:3]:
            if isinstance(idea, dict) and (idea.get("title") or "").strip():
                top_titles.append(str(idea.get("title")).strip()[:140])
        payload = {"top_long_titles": top_titles, "hours_window": int(data.get("hours_window") or 72) if isinstance(data, dict) else 72}
        db.add(SystemNotification(
            user_id=None,
            kind="topic_suggestions",
            title="Sugestões de temas",
            message=str(summary)[:900],
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="new",
        ))
    except Exception:
        pass
    db.commit()
    return {"generated_at": datetime.utcnow().isoformat(), "summary": str(summary)[:1200], "data": data}

@router.get("/monetization_status")
def get_monetization_status():
    """
    Análise de Monetização:
    - Resume progresso estimado rumo à monetização
    - Pede para a IA gerar diagnóstico + plano de ação
    """
    from app.services.ai_generator import AIContentGenerator
    yt = YouTubeService()
    ai = AIContentGenerator()

    progress = yt.get_monetization_progress()
    ai_insights = ai.generate_monetization_insights(progress)

    return {
        "progress": progress,
        "ai_insights": ai_insights,
    }

@router.get("/insights/subscribers")
def get_subscribers_insights(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    yt = YouTubeService()
    data = yt.get_subscriber_insights(days=days, max_results=20)
    kind = f"subscribers_{int(days)}d"
    db.add(ChannelInsight(
        user_id=None,
        kind=kind,
        start_date=None,
        end_date=None,
        data_json=json.dumps(data, ensure_ascii=False),
        ai_summary=None,
    ))
    db.commit()
    return data

@router.get("/notifications")
def list_notifications(db: Session = Depends(get_db)):
    return db.query(SystemNotification).order_by(SystemNotification.id.desc()).limit(50).all()

@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, db: Session = Depends(get_db)):
    n = db.query(SystemNotification).filter(SystemNotification.id == notification_id).first()
    if not n:
        raise HTTPException(status_code=404, detail="Notificação não encontrada")
    n.status = "read"
    n.read_at = datetime.utcnow()
    db.commit()
    db.refresh(n)
    return n

class CommunityReplyRequest(BaseModel):
    youtube_parent_id: str
    text: str
    youtube_video_id: Optional[str] = None
    scheduled_video_id: Optional[int] = None

@router.get("/community/comments")
def get_community_comments(
    youtube_video_id: Optional[str] = Query(None),
    scheduled_id: Optional[int] = Query(None),
    classify: bool = Query(True),
    db: Session = Depends(get_db)
):
    yt = YouTubeService()
    ai = AIContentGenerator()

    yid = (youtube_video_id or "").strip()
    if not yid and scheduled_id:
        sv = db.query(ScheduledVideo).filter(ScheduledVideo.id == scheduled_id).first()
        if not sv:
            raise HTTPException(status_code=404, detail="Vídeo agendado não encontrado")
        if not sv.youtube_video_id:
            raise HTTPException(status_code=400, detail="Vídeo ainda não possui youtube_video_id (não publicado).")
        yid = sv.youtube_video_id
    if not yid:
        raise HTTPException(status_code=400, detail="Informe youtube_video_id ou scheduled_id")

    try:
        raw_comments = yt.list_video_comments(yid, max_results=100)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    reply_by_owner = {}
    for c in raw_comments or []:
        try:
            if c.get("youtube_parent_id") and c.get("author_is_channel_owner"):
                pid = c.get("youtube_parent_id")
                if pid and pid not in reply_by_owner:
                    reply_by_owner[pid] = c
        except Exception:
            continue

    results = []
    for c in raw_comments:
        top_level = c.get("youtube_parent_id") is None
        label = None
        sentiment = None
        urgency = None
        draft = None
        status = "new"
        reply_text = None
        reply_sent_at = None

        existing = db.query(CommunityComment).filter(CommunityComment.youtube_comment_id == c["youtube_comment_id"]).first()
        if existing:
            label = existing.label
            sentiment = existing.sentiment
            urgency = existing.urgency
            draft = existing.reply_draft
            status = existing.status
            reply_text = existing.reply_text
            reply_sent_at = existing.reply_sent_at.isoformat() if getattr(existing, "reply_sent_at", None) else None

        if classify and top_level:
            try:
                sys = "Você é um assistente pastoral. Classifique e redija resposta empática, breve e bíblica quando apropriado."
                prompt = f"""
Analise o comentário abaixo e devolva JSON com as chaves:
- label: uma de [elogio, duvida, critica, pedido_oracao, testemunho, sugestao_tema, spam, toxico]
- sentiment: positive|neutral|negative
- urgency: low|medium|high
- draft_reply: texto breve (PT-BR), respeitoso, sem promessas irreais, citando referência bíblica opcional.

Comentário: \"\"\"{(c.get('text') or '').strip()}\"\"\"
"""
                raw = ai._generate_text(prompt, system_prompt=sys, json_mode=True)
                data = json.loads(raw or "{}")
                label = (data.get("label") or "").strip() or label
                sentiment = (data.get("sentiment") or "").strip() or sentiment
                urgency = (data.get("urgency") or "").strip() or urgency
                draft = (data.get("draft_reply") or "").strip() or draft
                status = "reviewed"
            except Exception:
                pass

        def _parse_dt(s: Optional[str]):
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
            except Exception:
                return None

        if top_level:
            try:
                rep = reply_by_owner.get(c.get("youtube_comment_id"))
                if rep:
                    status = "replied"
                    reply_text = (rep.get("text") or "").strip() or reply_text
                    dt = _parse_dt(rep.get("published_at"))
                    reply_sent_at = dt.isoformat() if dt else reply_sent_at
            except Exception:
                pass

        if existing:
            existing.youtube_parent_id = c.get("youtube_parent_id")
            existing.youtube_video_id = c.get("youtube_video_id")
            existing.author = c.get("author")
            existing.text = c.get("text")
            existing.like_count = int(c.get("like_count") or 0)
            existing.published_at = _parse_dt(c.get("published_at"))
            existing.label = label
            existing.sentiment = sentiment
            existing.urgency = urgency
            if draft:
                existing.reply_draft = draft
            if status:
                existing.status = status
            if top_level and status == "replied":
                if reply_text:
                    existing.reply_text = reply_text
                try:
                    existing.reply_sent_at = _parse_dt(reply_sent_at) if isinstance(reply_sent_at, str) else existing.reply_sent_at
                except Exception:
                    pass
        else:
            rec = CommunityComment(
                youtube_comment_id=c.get("youtube_comment_id"),
                youtube_parent_id=c.get("youtube_parent_id"),
                youtube_video_id=c.get("youtube_video_id"),
                scheduled_video_id=scheduled_id,
                author=c.get("author"),
                text=c.get("text"),
                like_count=int(c.get("like_count") or 0),
                published_at=_parse_dt(c.get("published_at")),
                status=status,
                sentiment=sentiment,
                label=label,
                urgency=urgency,
                reply_draft=draft,
                reply_text=reply_text if (top_level and status == "replied") else None,
                reply_sent_at=_parse_dt(reply_sent_at) if (top_level and status == "replied") else None,
            )
            db.add(rec)
        results.append({
            "youtube_comment_id": c.get("youtube_comment_id"),
            "youtube_parent_id": c.get("youtube_parent_id"),
            "youtube_video_id": c.get("youtube_video_id"),
            "author": c.get("author"),
            "text": c.get("text"),
            "like_count": int(c.get("like_count") or 0),
            "published_at": c.get("published_at"),
            "status": status,
            "label": label,
            "sentiment": sentiment,
            "urgency": urgency,
            "reply_draft": draft,
            "reply_text": reply_text,
            "reply_sent_at": reply_sent_at,
        })
    db.commit()
    return {"youtube_video_id": yid, "count": len(results), "items": results}

@router.post("/community/reply")
def post_community_reply(req: CommunityReplyRequest, db: Session = Depends(get_db)):
    yt = YouTubeService()
    if not req.youtube_parent_id or not req.text:
        raise HTTPException(status_code=400, detail="Campos obrigatórios: youtube_parent_id e text")
    try:
        yt.reply_to_comment(req.youtube_parent_id, req.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    top = db.query(CommunityComment).filter(CommunityComment.youtube_comment_id == req.youtube_parent_id).first()
    if top:
        top.status = "replied"
        top.reply_text = req.text
        top.reply_sent_at = datetime.utcnow()
        db.commit()
    return {"status": "sent"}


@router.post("/community/auto-thanks/run")
def run_auto_thanks(
    limit: Optional[int] = Query(None),
    backfill: bool = Query(False),
    db: Session = Depends(get_db),
):
    return auto_thank_comments(db, backfill=bool(backfill), limit=limit)

@router.post("/generate_video")
def generate_video(request: VideoRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user: Optional[User] = Depends(get_current_admin_user)):
    """Gera um vídeo motivacional e opcionalmente faz upload.

    Fluxo ÚNICO: UnifiedVideoPipeline do História/Devocional.
    - Monta UnifiedVideoRequest padronizado.
    - submit_or_reuse (cria UnifiedVideo + VideoTask com idempotência estrita 1:1).
    - kick async executa o executor que atualiza UnifiedVideo em cada etapa
      e no final chama validate_before_awaiting_review central.

    Não existe fallback para ``VideoGenerator``/``VideoFactory`` fora desse
    pipeline. Se o serviço central não carregar, a requisição falha fechada.
    """
    if _cancel_all_active():
        raise HTTPException(status_code=409, detail="Encerramento geral em andamento no servidor. Aguarde ~1 minuto e tente novamente.")

    try:
        payload = request.model_dump()
    except Exception:
        try:
            payload = request.model_dump(mode="python")
        except Exception:
            payload = request.dict()
    identity = _build_video_generation_identity(payload)
    payload["idempotency_key"] = identity["idempotency_key"]
    payload["request_hash"] = identity["request_hash"]
    content_hash = _payload_content_hash(payload)
    if content_hash:
        payload["content_hash"] = content_hash
    user_id = int(getattr(current_user, "id", None) or 0) or None

    # ====== Dedupe GLOBAL POR CONTEÚDO (últimas 48h) ======
    # Proteção extra anti-gasto: mesmo que idempotency_key tenha mudado
    # (ex: usuário clicou novamente após "Nova geração liberada", ou
    # microtime/UA varia), se o conteúdo (título/texto/minutos/kind) é
    # idêntico e já tem vídeo COMPLETED e válido, REUTILIZA esse resultado,
    # NÃO gera nova renderização → evita MP4 duplicado e custo extra.
    if not bool(getattr(request, "force_regenerate", False)):
        try:
            _reused_candidate = _find_reusable_completed_task_by_content(
                db, payload, excluded_task_id=None
            )
            if _reused_candidate:
                _rt = _reused_candidate
                _return_result = None
                try:
                    _ro = _rt.get("result_obj") or {}
                    _return_result = _ro if isinstance(_ro, dict) else {}
                    if "video_url" not in _return_result and _rt.get("video_url"):
                        _return_result["video_url"] = _rt["video_url"]
                    if "file_path" not in _return_result and _rt.get("file_path"):
                        _return_result["file_path"] = _rt["file_path"]
                except Exception:
                    _return_result = None
                return {
                    "message": (
                        "Vídeo já gerado para este conteúdo (últimas "
                        f"{int(_window_hours_content_reuse() or 48)}h). "
                        "Reutilizando resultado existente — NOVA renderização NÃO foi criada."
                    ),
                    "task_id": _rt.get("task_id"),
                    "queued": False,
                    "queue_position": 0,
                    "reused_existing_task": True,
                    "reused_completed_task": True,
                    "reused_by_content_hash": bool(content_hash),
                    "content_hash": content_hash,
                    "idempotency_key": str(identity.get("idempotency_key")),
                    "request_hash": identity.get("request_hash"),
                    "result": _return_result,
                    "pipeline": "content_reuse",
                }
        except Exception:
            pass

    # ====== Pipeline canônico obrigatório ======
    unified_ok = bool(_unified_enabled() and unified_video_pipeline is not None and UnifiedVideoRequest is not None)
    if not unified_ok:
        try:
            from app.config import unified_pipeline_required_error
        except Exception:
            unified_pipeline_required_error = (  # type: ignore[misc]
                lambda _module, _err=None: f"UnifiedVideoPipeline indisponível. Erro: {(_UNIFIED_IMPORT_ERR or 'unknown')[:500]}"
            )
        raise HTTPException(
            status_code=503,
            detail=unified_pipeline_required_error("story_generate_video", _UNIFIED_IMPORT_ERR),
        )

    unified_req = _build_unified_request_from_legacy(payload, user_id=user_id, module="story")
    if unified_req is None:
        raise HTTPException(status_code=422, detail="Payload inválido para o UnifiedVideoPipeline canônico.")
    try:
        kick_cb = _kick_story_video_task_queue_async if callable(_kick_story_video_task_queue_async) else None
        base_result = {
            "payload": payload,
            "kind": "youtube_story_video",
            "title_hint": _story_video_task_title_from_payload(payload),
            "idempotency_key": identity["idempotency_key"],
            "request_hash": identity["request_hash"],
            "source_module": "story",
            "pipeline": "unified_video_pipeline",
            "content_hash": content_hash or None,
        }
        res = unified_video_pipeline().submit_or_reuse(
            db,
            request=unified_req,
            kick_queue_callback=kick_cb,
            legacy_initial_result=base_result,
            user=current_user,
        )
        return {
            "message": res.message,
            "task_id": res.task_id,
            "queued": bool(res.queue_position > 1 or res.already_processing),
            "queue_position": int(res.queue_position or 0),
            "reused_existing_task": bool(res.reused_existing),
            "reused_completed_task": bool(res.reused_completed),
            "idempotency_key": str(res.idempotency_key),
            "request_hash": identity["request_hash"],
            "result": {
                "video_url": res.video_url,
                "youtube_video_id": res.youtube_video_id,
                "providers": res.providers,
                "unified_video_id": res.unified_video_id,
                "pipeline": "unified_video_pipeline",
            } if res.reused_completed else None,
            "pipeline": "unified_video_pipeline",
        }
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"UnifiedVideoPipeline falhou no submit: {type(exc).__name__}: {str(exc)[:300]}")


# Dependência segura: retorna None se o token for inválido (sem bloquear clients RQ/scheduler).
def get_current_admin_user_safe(request: Request):
    from app.routers.auth import get_current_admin_user as _inner_admin
    try:
        user = _inner_admin(request)
        if user:
            return user
    except Exception:
        return None
    return None

@router.get("/task/{task_id}")
def get_task_status(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    task = dict(task)
    if str(task.get("status") or "").strip().lower() == "pause_requested" and not _task_executor_is_alive(task_id):
        paused = mark_task_paused(
            task_id,
            message="Produção pausada com segurança; o executor foi encerrado e o servidor está livre.",
        )
        task = dict(paused or get_task(task_id) or task)
        _kick_story_video_task_queue_async()
    try:
        task_status = str((task.get("status") or "")).lower()
        if task_status in {"processing"}:
            updated_at_s = (task.get("updated_at") or task.get("created_at") or "").strip()
            if updated_at_s:
                try:
                    dt = datetime.fromisoformat(updated_at_s.replace("Z", "+00:00"))
                    if getattr(dt, "tzinfo", None) is not None:
                        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                except Exception:
                    dt = None
                if dt:
                    try:
                        stale_minutes = int((os.getenv("VIDEO_TASK_STALE_MINUTES") or "").strip() or "180")
                    except Exception:
                        stale_minutes = 180
                    stale_minutes = max(30, min(7 * 24 * 60, stale_minutes))
                    if datetime.utcnow() - dt > timedelta(minutes=stale_minutes):
                        try:
                            p = int(task.get("progress") or 0)
                        except Exception:
                            p = 0
                        if 0 < p < 100:
                            update_task(
                                task_id,
                                status="failed",
                                progress=p,
                                message=f"Tarefa travada: sem atualização há mais de {stale_minutes} min. Use 'Reiniciar tarefa' ou 'Encerrar no servidor'.",
                            )
                            task = get_task(task_id) or task
    except Exception:
        pass
    try:
        task["runtime"] = _runtime_view_for_task(task)
        result_obj = task.get("result") if isinstance(task.get("result"), dict) else {}
        telemetry_obj = result_obj.get("runtime_telemetry") if isinstance(result_obj.get("runtime_telemetry"), dict) else {}
        signal_age = task["runtime"].get("last_signal_seconds")
        if (
            str(task.get("status") or "").lower() == "processing"
            and str(task["runtime"].get("state") or "") == "possibly_interrupted"
            and signal_age is not None
            and int(signal_age) >= (
                _runtime_interruption_seconds()
                if int(telemetry_obj.get("version") or 0) >= 1
                else max(15 * 60, _runtime_interruption_seconds())
            )
        ):
            monitored = int(telemetry_obj.get("version") or 0) >= 1
            update_task(
                task_id,
                status="failed",
                progress=int(task.get("progress") or 0),
                message=(
                    f"Produção interrompida: o executor não envia sinais há {int(signal_age)} segundos. "
                    + (
                        "O monitor registrou a interrupção e preservou os dados; use Reiniciar tarefa para continuar."
                        if monitored
                        else "A execução antiga perdeu o executor; os dados foram preservados e a tarefa pode ser reiniciada."
                    )
                ),
                result=result_obj,
            )
            task = dict(get_task(task_id) or task)
            task["runtime"] = _runtime_view_for_task(task)
    except Exception:
        task["runtime"] = {
            "state": "unknown",
            "label": "Monitoramento indisponível",
            "detail": "Não foi possível calcular a atividade do processo neste instante.",
        }
    return task


@router.get("/tasks/by-idempotency")
def get_task_status_by_idempotency(idempotency_key: str = Query(..., min_length=10)):
    task = get_task_by_idempotency_key(idempotency_key)
    if not task:
        raise HTTPException(status_code=404, detail="Nenhuma tarefa encontrada para esta idempotency_key.")
    return task


@router.get("/guardian/overview")
def get_financial_guardian_overview(
    period: str = Query("today"),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    return youtube_financial_guardian_observability_service.build_overview(
        db,
        user=current_user,
        period=period,
    )


@router.get("/guardian/timeline/{task_id}")
def get_financial_guardian_timeline(
    task_id: str,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    payload = youtube_financial_guardian_observability_service.build_timeline(
        db,
        user=current_user,
        task_id=task_id,
    )
    if not payload.get("found"):
        raise HTTPException(status_code=404, detail="Timeline financeira nao encontrada para esta tarefa.")
    return payload


@router.post("/guardian/preestimate")
def get_financial_guardian_preestimate(
    payload: Dict[str, Any],
    current_user: User = Depends(get_current_admin_user),
):
    return youtube_financial_guardian_observability_service.estimate_preproduction(
        user=current_user,
        payload=payload or {},
    )


@router.post("/guardian/simulate/{scenario_code}")
def simulate_financial_guardian_scenario(
    scenario_code: str,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    try:
        return youtube_financial_guardian_observability_service.simulate_scenario(
            db,
            user=current_user,
            scenario_code=scenario_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/guardian/ledger")
def list_financial_guardian_ledger(
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    return {
        "items": youtube_financial_guardian_observability_service.list_ledger_entries(
            db,
            user=current_user,
        )
    }


@router.post("/guardian/ledger")
def create_financial_guardian_ledger_entry(
    payload: Dict[str, Any],
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    return youtube_financial_guardian_observability_service.save_ledger_entry(
        db,
        user=current_user,
        payload=payload or {},
    )


@router.put("/guardian/ledger/{entry_id}")
def update_financial_guardian_ledger_entry(
    entry_id: int,
    payload: Dict[str, Any],
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    return youtube_financial_guardian_observability_service.save_ledger_entry(
        db,
        user=current_user,
        payload=payload or {},
        entry_id=entry_id,
    )


@router.delete("/guardian/ledger/{entry_id}")
def delete_financial_guardian_ledger_entry(
    entry_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    youtube_financial_guardian_observability_service.delete_ledger_entry(
        db,
        user=current_user,
        entry_id=entry_id,
    )
    return {"status": "deleted", "id": entry_id}

@router.get("/tasks/active")
def list_active_tasks(limit: int = 10, _admin=Depends(get_current_admin_user)):
    db = SessionLocal()
    try:
        lim = max(1, min(50, int(limit or 10)))
        rows = (
            db.query(VideoTask)
            .filter(VideoTask.status.in_(["pending", "processing"]))
            .order_by(VideoTask.updated_at.desc().nullslast(), VideoTask.created_at.desc().nullslast())
            .limit(lim)
            .all()
        )
        items = []
        for r in rows:
            items.append({
                "task_id": r.id,
                "status": r.status,
                "progress": int(r.progress or 0),
                "message": r.message,
                "created_at": (r.created_at.isoformat() if getattr(r, "created_at", None) else None),
                "updated_at": (r.updated_at.isoformat() if getattr(r, "updated_at", None) else None),
            })
        return {"count": len(items), "items": items}
    finally:
        db.close()

@router.get("/tasks/queue")
def list_story_video_task_queue(limit: int = 20, _admin=Depends(get_current_admin_user)):
    db = SessionLocal()
    try:
        rows = _load_story_video_task_rows(db, limit=limit, include_paused=True)
        orphaned_pause = False
        for row in rows:
            if str(row.status or "").strip().lower() != "pause_requested":
                continue
            if not _task_executor_is_alive(str(row.id)):
                mark_task_paused(
                    str(row.id),
                    message="Produção pausada com segurança; o executor foi encerrado e o servidor está livre.",
                )
                orphaned_pause = True
        if orphaned_pause:
            db.expire_all()
            rows = _load_story_video_task_rows(db, limit=limit, include_paused=True)
            _kick_story_video_task_queue_async()
        factory_busy = bool(_is_video_factory_busy())
        task_ids = {str(row.id) for row in rows}
        items: List[Dict[str, Any]] = [
            _story_video_task_item_from_row(row, index)
            for index, row in enumerate(rows, start=1)
        ]
        items.extend(
            _load_production_video_queue_items(
                db,
                excluded_task_ids=task_ids,
                limit=max(50, int(limit or 20)),
            )
        )

        if factory_busy and not any(bool(item.get("is_current")) for item in items):
            blocker = _load_factory_blocker_item(db, excluded_task_ids=task_ids)
            blocker_key = None
            if blocker:
                blocker_key = (
                    str(blocker.get("source_type") or ""),
                    str(blocker.get("task_id") or blocker.get("production_video_id") or ""),
                )
            existing_keys = {
                (
                    str(item.get("source_type") or ""),
                    str(item.get("task_id") or item.get("production_video_id") or ""),
                )
                for item in items
            }
            if blocker and blocker_key not in existing_keys:
                items.append(blocker)

        status_order = {
            "processing": 0,
            "pause_requested": 0,
            "pending": 1,
            "paused": 2,
        }
        items.sort(key=lambda item: (
            status_order.get(str(item.get("status") or "").strip().lower(), 3),
            str(item.get("created_at") or ""),
            str(item.get("task_id") or item.get("production_video_id") or ""),
        ))

        if not items:
            recoverable = _load_latest_recoverable_story_video_task(db)
            if recoverable is not None:
                recovery_item = _story_video_task_item_from_row(recoverable, len(items) + 1)
                recovery_item.update({
                    "is_current": False,
                    "source_label": "Histórico recuperável — não é uma nova produção",
                    "queue_label": "Falha antiga — abra ou reinicie somente se desejar",
                    "can_open": True,
                    "can_cancel": False,
                    "recoverable": True,
                    "auto_open": False,
                })
                items.append(recovery_item)
        items = items[: max(1, min(200, int(limit or 20)))]
        runnable_position = 0
        for idx, item in enumerate(items, start=1):
            item["position"] = idx
            normalized_status = str(item.get("status") or "").strip().lower()
            if normalized_status in {"processing", "pause_requested", "pending"}:
                runnable_position += 1
                item["queue_position"] = runnable_position
            else:
                item["queue_position"] = None
        payload = {
            "count": len(items),
            "processing_count": len([i for i in items if bool(i.get("is_current"))]),
            "paused_count": len([i for i in items if str(i.get("status") or "").lower() == "paused"]),
            "queued_count": len([i for i in items if str(i.get("status") or "").lower() == "pending"]),
            "occupiers": [i for i in items if bool(i.get("is_current")) or str(i.get("queue_label") or "").lower() == "ocupando o servidor"],
            "items": items,
            "next_task": next(
                (i for i in items if str(i.get("status") or "").lower() == "pending"),
                None,
            ),
        }
        payload["factory_busy"] = factory_busy
        return payload
    finally:
        db.close()

@router.get("/task/{task_id}/watch", response_class=HTMLResponse)
def watch_task_video(task_id: str, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    _require_user_from_query_token(token, db)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    status = str((task.get("status") or "")).lower()
    if status not in {"completed", "rendered_upload_failed"}:
        raise HTTPException(status_code=409, detail="Tarefa ainda não concluída.")
    result = task.get("result") or {}
    video_url = None
    if isinstance(result, dict):
        video_url = result.get("video_url") or result.get("videoUrl")
    video_url = _normalize_video_url_for_client(video_url)
    if not video_url:
        raise HTTPException(status_code=404, detail="URL do vídeo não encontrada.")
    html = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Assistir vídeo</title>
  <style>
    body {{ margin: 0; background: #000; }}
    .wrap {{ width: 100vw; height: 100vh; display: flex; align-items: center; justify-content: center; }}
    video {{ width: 100%; height: 100%; max-width: 1280px; max-height: 100vh; background: #000; }}
  </style>
</head>
<body>
  <div class="wrap">
    <video controls autoplay playsinline src="{video_url}"></video>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)

@router.get("/task/{task_id}/media")
def watch_task_video_media(task_id: str, request: Request, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    _require_user_from_query_token(token, db)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    status = str((task.get("status") or "")).lower()
    if status not in {"completed", "rendered_upload_failed"}:
        raise HTTPException(status_code=409, detail="Tarefa ainda não concluída.")
    result = task.get("result") or {}
    video_url = None
    if isinstance(result, dict):
        video_url = result.get("video_url") or result.get("videoUrl")
    video_url = _normalize_video_url_for_client(video_url)
    if not video_url:
        raise HTTPException(status_code=404, detail="URL do vídeo não encontrada.")

    if str(video_url).startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL do vídeo não é local ao servidor.")

    path = _resolve_video_file_path(video_url)
    if not path:
        raise HTTPException(status_code=404, detail="Video file not found")
    inline_name = os.path.basename(path) or f"task_{task_id}.mp4"
    return _video_range_response(request, path, inline_filename=inline_name)

@router.post("/task/{task_id}/cancel")
def cancel_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    status = str((task.get("status") or "")).lower()
    if status in {"completed", "failed", "cancelled"}:
        return {"message": "Nada a cancelar", "task_id": task_id, "status": status}
    try:
        current_progress = int(task.get("progress") or 0)
    except Exception:
        current_progress = 0
    request_cancel_task(task_id, message="Cancelado pelo usuário.")
    update_task(task_id, status="cancelled", progress=current_progress, message="Cancelado pelo usuário.")
    _kick_story_video_task_queue_async()
    return {"message": "Cancelado", "task_id": task_id, "status": "cancelled"}


@router.post("/task/{task_id}/pause")
def pause_task(task_id: str, _admin=Depends(get_current_admin_user)):
    """Pausa a tarefa no próximo checkpoint e preserva todos os ativos."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    status = str(task.get("status") or "").strip().lower()
    if status == "paused":
        return {
            "message": "A produção já está pausada.",
            "task_id": task_id,
            "status": "paused",
            "assets_preserved": True,
        }
    if status == "pause_requested":
        return {
            "message": "A pausa já foi solicitada e será aplicada após a etapa atual.",
            "task_id": task_id,
            "status": "pause_requested",
            "assets_preserved": True,
        }
    if status not in {"pending", "processing"}:
        raise HTTPException(status_code=409, detail="Apenas tarefas na fila ou em execução podem ser pausadas.")

    executor_alive = _task_executor_is_alive(task_id)
    immediate = bool(status == "pending" or not executor_alive)
    paused = request_pause_task(
        task_id,
        message="Pausa solicitada; a etapa atual será finalizada antes de liberar a próxima produção.",
        immediate=immediate,
    )
    if not paused:
        raise HTTPException(status_code=500, detail="Não foi possível solicitar a pausa da tarefa.")
    if immediate:
        _kick_story_video_task_queue_async()
    return {
        "message": paused.get("message") or "Pausa solicitada.",
        "task_id": task_id,
        "status": paused.get("status") or ("paused" if immediate else "pause_requested"),
        "assets_preserved": True,
        "pause_mode": "immediate" if immediate else "after_current_stage",
    }


@router.post("/task/{task_id}/discard")
def discard_failed_task(task_id: str, _admin=Depends(get_current_admin_user)):
    """Descarta somente uma tarefa falhada e libera uma geração realmente nova.

    Diferentemente de ``cancel_all``, esta ação não interfere em outras filas.
    O registro permanece no histórico para auditoria de custo, mas deixa de ser
    recuperável e a chave de deduplicação expira pelo fluxo de cancelamento.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    status = str((task.get("status") or "")).strip().lower()
    if status == "cancelled":
        return {
            "message": "Tarefa já estava descartada.",
            "task_id": task_id,
            "status": "cancelled",
            "discarded": True,
        }
    if status != "failed":
        raise HTTPException(
            status_code=409,
            detail="Somente tarefas com falha podem ser descartadas. Use Cancelar para tarefas em andamento.",
        )

    discarded = request_cancel_task(task_id, message="Tarefa falhada descartada pelo usuário.")
    if not discarded:
        raise HTTPException(status_code=500, detail="Não foi possível descartar a tarefa falhada.")

    # Mantém a máquina de estados canônica e eventual episódio de série
    # sincronizados. Falha auxiliar não pode transformar um descarte já
    # concluído em HTTP 500 e deixar a interface presa.
    sync_warnings: List[str] = []
    linked_episodes = 0
    pipeline_db = SessionLocal()
    try:
        try:
            if _unified_enabled() and unified_video_pipeline is not None:
                unified_video_pipeline().transition_status(
                    pipeline_db,
                    task_id,
                    status="cancelled",
                    step="discarded",
                    progress=int(task.get("progress") or 0),
                    message="Tarefa antiga descartada; uma nova geração está liberada.",
                    merge_result={"discarded": True, "discarded_task_id": task_id},
                )
        except Exception as exc:
            pipeline_db.rollback()
            sync_warnings.append(f"pipeline:{type(exc).__name__}")

        try:
            episodes = pipeline_db.query(SeriesEpisode).filter(SeriesEpisode.task_id == str(task_id)).all()
            series_ids = set()
            for episode in episodes:
                if str(episode.status or "").strip().lower() != "published":
                    episode.status = "cancelled"
                    linked_episodes += 1
                series_ids.add(int(episode.series_id))
            if series_ids:
                series_rows = pipeline_db.query(SeriesPlan).filter(SeriesPlan.id.in_(series_ids)).all()
                for series in series_rows:
                    if str(series.status or "").strip().lower() in {"active", "pending_issue"}:
                        series.status = "paused"
                pipeline_db.commit()
        except Exception as exc:
            pipeline_db.rollback()
            sync_warnings.append(f"series:{type(exc).__name__}")
    finally:
        pipeline_db.close()

    _kick_story_video_task_queue_async()
    return {
        "message": "Tarefa descartada. Uma nova geração está liberada.",
        "task_id": task_id,
        "status": "cancelled",
        "discarded": True,
        "linked_episodes_cancelled": linked_episodes,
        "sync_warnings": sync_warnings,
    }

@router.post("/tasks/cancel_all")
def cancel_all_tasks(_admin=Depends(get_current_admin_user)):
    barrier_token = _activate_cancel_all_barrier()
    if conn is not None and barrier_token is None and _cancel_all_active():
        raise HTTPException(
            status_code=409,
            detail="O encerramento geral já está em andamento. Aguarde a conclusão.",
        )
    try:
        return _cancel_all_tasks_snapshot()
    finally:
        _release_cancel_all_barrier(barrier_token)


def _cancel_all_tasks_snapshot():
    if conn:
        try:
            conn.delete(FACTORY_LOCK_KEY)
        except Exception:
            pass

    try:
        if os.path.exists(_FACTORY_LOCK_PATH):
            os.remove(_FACTORY_LOCK_PATH)
    except Exception:
        pass

    task_ids_to_cancel: List[str] = []
    db = SessionLocal()
    try:
        rows = db.query(VideoTask).filter(VideoTask.status.in_(["pending", "processing", "failed"])).all()
        for r in rows:
            status = str(r.status or "").strip().lower()
            result_obj = _video_task_result_obj(r)
            if status in {"pending", "processing"} or (
                status == "failed" and _is_story_video_generation_task(result_obj)
            ):
                task_ids_to_cancel.append(str(r.id))
        sv = db.query(ScheduledVideo).filter(ScheduledVideo.status.in_(["queued", "processing"])).all()
        for v in sv:
            v.status = "failed"
            v.progress = 0
            msg = "[CANCELADO]: Produção encerrada pelo usuário."
            if not (v.description and msg in v.description):
                v.description = ((v.description or "") + "\n\n" + msg).strip()[:5000]
        db.commit()
    finally:
        db.close()

    cancelled_task_ids: List[str] = []
    for task_id in sorted(set(task_ids_to_cancel)):
        cancelled = request_cancel_task(
            task_id,
            message="Cancelado pelo usuário (encerrar produção do servidor).",
        )
        if cancelled:
            cancelled_task_ids.append(task_id)

    series_stats = {"paused_series": 0, "cancelled_series_episodes": 0}
    series_db = SessionLocal()
    try:
        from app.services.youtube_series_service import youtube_series_service

        series_stats = youtube_series_service.pause_for_server_shutdown(
            series_db,
            cancelled_task_ids=cancelled_task_ids,
        )
    except Exception:
        series_db.rollback()
    finally:
        series_db.close()

    _kick_story_video_task_queue_async()
    return {
        "status": "ok",
        "message": "Produção encerrada, falhas antigas descartadas e séries ativas pausadas.",
        "cancelled_tasks": len(cancelled_task_ids),
        **series_stats,
    }

@router.get("/notifications")
def list_youtube_notifications(limit: int = 20, status: Optional[str] = None, _admin=Depends(get_current_admin_user)):
    db = SessionLocal()
    try:
        q = db.query(SystemNotification).filter(SystemNotification.kind.in_(["video_generated", "shorts_generated"]))
        if status:
            q = q.filter(SystemNotification.status == status)
        rows = q.order_by(SystemNotification.created_at.desc()).limit(max(1, min(100, int(limit or 20)))).all()
        items = []
        for n in rows:
            payload = None
            if n.payload_json:
                try:
                    payload = json.loads(n.payload_json)
                except Exception:
                    payload = n.payload_json
            items.append({
                "id": n.id,
                "kind": n.kind,
                "title": n.title,
                "message": n.message,
                "status": n.status,
                "created_at": (n.created_at.isoformat() if n.created_at else None),
                "read_at": (n.read_at.isoformat() if n.read_at else None),
                "payload": payload,
            })
        return {"count": len(items), "items": items}
    finally:
        db.close()

@router.post("/notifications/{notification_id}/read")
def mark_youtube_notification_read(notification_id: int, _admin=Depends(get_current_admin_user)):
    db = SessionLocal()
    try:
        n = db.query(SystemNotification).filter(SystemNotification.id == notification_id).first()
        if not n:
            raise HTTPException(status_code=404, detail="Notificação não encontrada")
        n.status = "read"
        n.read_at = datetime.utcnow()
        db.commit()
        return {"status": "read", "id": n.id}
    finally:
        db.close()

@router.post("/task/{task_id}/retry")
def retry_task(task_id: str, _admin=Depends(get_current_admin_user)):
    lock_info = None
    try:
        lock_info = acquire_distributed_lock(
            f"story-retry:{task_id}",
            timeout_seconds=5,
            ttl_seconds=30,
        )
        task = get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        status = str((task.get("status") or "")).lower()
        progress = task.get("progress")
        try:
            progress_n = int(progress) if progress is not None else 0
        except Exception:
            progress_n = 0
        updated_at_s = (task.get("updated_at") or task.get("created_at") or "").strip()
        task_dt = _task_payload_timestamp(updated_at_s)
        stale_minutes = _story_video_stale_minutes()
        is_stale_processing = bool(
            status == "processing"
            and task_dt
            and (datetime.utcnow() - task_dt > timedelta(minutes=stale_minutes))
        )
        if status == "completed":
            raise HTTPException(status_code=400, detail="Tarefa já concluída.")
        if is_stale_processing:
            stale_message = (
                f"Falha automática para retry seguro: tarefa travada sem atualização há mais de "
                f"{stale_minutes} min."
            )
            update_task(task_id, status="failed", progress=progress_n, message=stale_message)
            status = "failed"
        elif status in {"pending", "processing"}:
            return {
                "message": "A mesma tarefa já está na fila ou em processamento.",
                "task_id": task_id,
                "already_restarted": True,
                "reused_task": True,
            }

        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        saved_payload = (result or {}).get("payload") if isinstance(result, dict) else None
        if not isinstance(saved_payload, dict):
            raise HTTPException(status_code=400, detail="Não há payload salvo para reiniciar esta tarefa.")
        payload = dict(saved_payload)
        payload["force_reuse_assets"] = True
        payload.pop("force_render_only", None)
        payload = _maybe_enable_render_only_flags(payload, task_id)
        try:
            VideoRequest(**payload)
        except Exception:
            raise HTTPException(status_code=400, detail="Payload inválido para reiniciar a tarefa.")

        if status == "paused":
            merge_task_result(task_id, {
                "payload": payload,
                "resume_requested_at": datetime.utcnow().isoformat(),
            })
            resumed = enqueue_paused_task_for_resume(
                task_id,
                message="Produção retomada e recolocada na fila; aguardando sua vez.",
            )
            if not resumed or str(resumed.get("status") or "").lower() != "pending":
                raise HTTPException(status_code=500, detail="Não foi possível recolocar a tarefa pausada na fila.")

            pipeline_sync_warning = None
            pipeline_db = SessionLocal()
            try:
                unified_video_pipeline().transition_status(
                    pipeline_db,
                    task_id,
                    status="pending",
                    step="queued_resume",
                    progress=progress_n,
                    message="Produção retomada pelo usuário e aguardando sua vez na fila.",
                    merge_result={
                        "recovery": {
                            "same_task": True,
                            "resumed_from_pause": True,
                            "force_reuse_assets": True,
                        }
                    },
                )
            except Exception as exc:
                pipeline_db.rollback()
                pipeline_sync_warning = f"{type(exc).__name__}: {str(exc)[:160]}"
            finally:
                pipeline_db.close()

            _kick_story_video_task_queue_async()
            return {
                "message": "Produção retomada e recolocada na fila. Ela iniciará quando chegar sua vez.",
                "task_id": task_id,
                "status": "pending",
                "queued": True,
                "reused_task": True,
                "reuse_assets": True,
                "pipeline": "unified_video_pipeline",
                "pipeline_sync_warning": pipeline_sync_warning,
            }

        resume_progress = max(1, progress_n)
        reset = reset_task_for_retry(
            task_id,
            progress=resume_progress,
            message="Retomada preparada com reaproveitamento dos ativos; aguardando worker CX33...",
        )
        if not reset:
            raise HTTPException(status_code=500, detail="Não foi possível preparar a tarefa para recuperação.")

        pipeline_db = SessionLocal()
        try:
            uv = unified_video_pipeline().transition_status(
                pipeline_db,
                task_id,
                status="pending",
                step="queued_recovery",
                progress=resume_progress,
                message="Retomada preparada, reutilizando os ativos disponíveis e aguardando worker CX33.",
                merge_result={
                    "recovery": {
                        "same_task": True,
                        "force_reuse_assets": True,
                        "force_render_only": bool(payload.get("force_render_only")),
                    }
                },
            )
            if uv is not None:
                uv.force_reuse_assets = True
                uv.force_render_only = bool(payload.get("force_render_only"))
                pipeline_db.commit()
        finally:
            pipeline_db.close()

        _dispatch_video_generation_task(payload, task_id)
        return {
            "message": "Mesma tarefa reiniciada com reaproveitamento de ativos.",
            "task_id": task_id,
            "reused_task": True,
            "reuse_assets": True,
            "render_only": bool(payload.get("force_render_only")),
            "pipeline": "unified_video_pipeline",
        }
    except TimeoutError:
        raise HTTPException(status_code=409, detail="A recuperação desta tarefa já está sendo iniciada.")
    finally:
        release_distributed_lock(lock_info)

@router.get("/diagnostics/video_generation")
def diagnose_video_generation(task_id: Optional[str] = None, ai: bool = False, _admin=Depends(get_current_admin_user)):
    import shutil
    
    checks = []

    use_rq_raw = (os.getenv("USE_RQ_FOR_VIDEO_GENERATION") or "").strip()
    use_rq = use_rq_raw.lower() in {"1", "true", "yes"}
    checks.append({"name": "USE_RQ_FOR_VIDEO_GENERATION", "ok": True, "value": use_rq_raw or "(não definido)"})

    redis_ok = conn is not None
    checks.append({"name": "Redis (conn)", "ok": redis_ok})

    workers = _rq_workers_online()
    checks.append({"name": "RQ workers online", "ok": bool(workers)})

    ffmpeg_path = shutil.which("ffmpeg")
    checks.append({"name": "ffmpeg no PATH", "ok": bool(ffmpeg_path), "value": ffmpeg_path})

    magick_path = shutil.which("magick") or shutil.which("convert")
    checks.append({"name": "ImageMagick no PATH", "ok": bool(magick_path), "value": magick_path})

    resource_health: Dict[str, Any] = {}
    try:
        from app.services.video_resource_guard import (
            capture_resource_snapshot,
            evaluate_runtime_resource_health,
        )
        resource_health = evaluate_runtime_resource_health(capture_resource_snapshot())
        resources = resource_health.get("snapshot") if isinstance(resource_health.get("snapshot"), dict) else {}
        checks.extend([
            {
                "name": "Memória RAM",
                "ok": str(resource_health.get("level") or "") != "critical",
                "value": f"Disponível: {float(resources.get('available_memory_mb') or 0):.0f} MB / Total: {float(resources.get('total_memory_mb') or 0):.0f} MB",
            },
            {
                "name": "Swap",
                "ok": float(resources.get("swap_used_percent") or 0) < 95.0,
                "value": f"Em uso: {float(resources.get('swap_used_percent') or 0):.1f}%",
            },
            {
                "name": "Espaço para mídia",
                "ok": float(resources.get("disk_free_gb") or 0) > 2.0,
                "value": f"Livre: {float(resources.get('disk_free_gb') or 0):.1f} GB em {resources.get('disk_path') or '/'}",
            },
            {
                "name": "Carga do servidor",
                "ok": float(resources.get("load_per_cpu") or 0) < 3.0,
                "value": f"{float(resources.get('load_per_cpu') or 0):.2f} por CPU",
            },
            {
                "name": "Memória do processo",
                "ok": True,
                "value": f"Atual: {float(resources.get('process_rss_mb') or 0):.0f} MB / Pico: {float(resources.get('process_peak_rss_mb') or 0):.0f} MB",
            },
        ])
    except Exception:
        pass

    report: Dict[str, Any] = {
        "task_id": task_id,
        "checks": checks,
        "task": None,
        "recommendations": [],
        "resource_health": resource_health,
        "ai": None,
    }

    if task_id:
        t = get_task(task_id)
        report["task"] = t
        if not t:
            report["recommendations"].append("Task não encontrada: confirme se o deploy é o mesmo servidor e se o task_id é válido.")
        else:
            t = dict(t)
            runtime = _runtime_view_for_task(t)
            t["runtime"] = runtime
            report["task"] = t
            status = str((t.get("status") or "")).lower()
            msg = str((t.get("message") or ""))
            if status == "failed":
                report["recommendations"].append(
                    f"Tarefa falhou em {int(t.get('progress') or 0)}%: {msg or 'sem mensagem técnica registrada.'}"
                )
            elif status == "pending" and msg:
                report["recommendations"].append(f"Tarefa aguardando execução: {msg}")
            runtime_state = str(runtime.get("state") or "")
            if runtime_state == "working":
                report["recommendations"].append("Produção ativa: o executor continua enviando sinais ao servidor.")
            elif runtime_state in {"resource_warning", "resource_pressure"}:
                report["recommendations"].append(
                    "A produção está ativa, mas os recursos estão sob pressão: "
                    + " ".join(runtime.get("resource_reasons") or [])
                )
            elif runtime_state == "delayed":
                report["recommendations"].append(
                    "O sinal está atrasado, mas ainda dentro do limite de interrupção de "
                    f"{int(runtime.get('interruption_threshold_seconds') or _runtime_interruption_seconds())} segundos."
                )
            elif runtime_state == "possibly_interrupted":
                report["recommendations"].append(
                    "O executor ultrapassou o limite sem sinais; verifique timeout do provedor, "
                    "reinício do contêiner ou encerramento do processo."
                )
            if status in {"pending", "processing"} and ("enfileirando" in msg.lower() or "separado" in msg.lower() or (t.get("progress") in (0, 1))):
                report["recommendations"].append("O processo parece travado no início. Use Reiniciar para recuperar a tarefa; vídeos longos serão executados em processo isolado para proteger a API.")
            if use_rq and (not workers):
                report["recommendations"].append("O worker RQ/CX33 não está disponível. A produção pesada permanecerá preservada e não será executada no servidor principal; restabeleça o worker CX33/Redis e reinicie a tarefa.")
            if (not ffmpeg_path):
                report["recommendations"].append("ffmpeg não encontrado no PATH. Instale/adicione ffmpeg (moviepy precisa).")

    if ai:
        try:
            ai_service = AIContentGenerator()
            prompt = (
                "Você é um engenheiro de suporte de infraestrutura diagnosticando um sistema de geração de vídeos em Python (MoviePy).\n"
                "O usuário diz que o sistema está travando e não gera vídeos. Analise os dados abaixo:\n"
                "- O sistema pode estar sem memória RAM livre (< 300MB é crítico para vídeo).\n"
                "- A tarefa pode ter tentado iniciar em um processo separado (multiprocessing) e falhado devido a falta de Redis ou falha do SO.\n"
                "- Ferramentas como ffmpeg e ImageMagick precisam estar presentes.\n"
                "Retorne um JSON estrito respondendo:\n"
                "{ \"causas_provaveis\": [\"...\"], \"acoes_recomendadas\": [\"...\"], \"acoes_seguras_no_sistema\": [\"...\"] }\n\n"
                f"RELATÓRIO DO SISTEMA:\n{json.dumps(report, ensure_ascii=False)}"
            )
            raw = (ai_service._generate_text(prompt, system_prompt="Responda apenas JSON válido e direto ao ponto em português.", json_mode=True) or "").strip()
            try:
                report["ai"] = json.loads(raw) if raw else None
            except Exception:
                report["ai"] = {"raw": raw}
        except Exception as e:
            report["ai"] = {"error": str(e)}

    return report

def process_video_generation_payload(payload: Dict[str, Any], task_id: str):
    resource_report = _series_resource_preflight(payload, task_id)
    if resource_report is not None and not bool(resource_report.get("allowed")):
        return
    _prefer_renderer_as_oom_victim(payload, task_id)
    try:
        req = VideoRequest(**(payload or {}))
    except Exception:
        update_task(task_id, status="failed", progress=0, message="Payload inválido para geração de vídeo.")
        return
    process_video_generation(req, task_id)

def process_video_generation(request: VideoRequest, task_id):
    # Lazy import VideoGenerator (moviepy/PIL/numpy) para reduzir memória no startup
    from app.services.video_generator import VideoGenerator

    class _TaskCancelled(Exception):
        pass

    class _TaskPaused(Exception):
        pass

    def _raise_if_cancelled():
        t = get_task(task_id) or {}
        status = str((t.get("status") or "")).lower()
        if status == "cancelled" or _cancel_all_active():
            raise _TaskCancelled()
        if status in {"pause_requested", "paused"} or is_task_pause_requested(task_id):
            raise _TaskPaused()

    def _merged_task_result(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        current = get_task(task_id) or {}
        base = current.get("result") if isinstance(current.get("result"), dict) else {}
        merged = dict(base or {})
        if extra:
            merged.update(extra)
        return merged

    executor_id = f"executor-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    lease_info: Dict[str, Any] = {}
    redis_lock = None
    file_lock = None
    guardian_db = None
    guardian_payload: Dict[str, Any] = {}
    guardian_user_id: Optional[int] = None
    guardian_context = None
    guardian_preflight: Optional[Dict[str, Any]] = None
    guardian_cache_summary: Dict[str, Any] = {"stored_assets": 0, "cache_keys": []}
    runtime_monitor_stop = None
    runtime_monitor_thread = None
    try:
        _raise_if_cancelled()
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("MKL_NUM_THREADS", "1")
        os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
        lease_info = acquire_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
        if not lease_info.get("acquired"):
            print(f"Tarefa {task_id} já possui executor ativo. Ignorando segundo disparo.")
            return
        _dbg_event("H5", "lease acquired", {"task_id": str(task_id), "executor_id": executor_id, "lease": lease_info})
        heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
        if conn:
            try:
                redis_lock = conn.lock(FACTORY_LOCK_KEY, timeout=4 * 60 * 60, blocking_timeout=1)
                if not redis_lock.acquire(blocking=False):
                    update_task(task_id, status="pending", progress=0, message="Servidor ocupado. Aguardando vez na fila de produção...")
                    _dbg_event("H5", "factory busy (redis lock)", {"task_id": str(task_id), "executor_id": executor_id})
                    return
            except Exception:
                redis_lock = None

        if not conn or not redis_lock:
            try:
                file_lock = FileLock(_FACTORY_LOCK_PATH, timeout=0)
                file_lock.acquire()
            except Timeout:
                update_task(task_id, status="pending", progress=0, message="Servidor ocupado. Aguardando vez na fila de produção...")
                _dbg_event("H5", "factory busy (file lock)", {"task_id": str(task_id), "executor_id": executor_id})
                return
            except Exception:
                file_lock = None

        kind_norm = (request.kind or "").strip().lower()
        if kind_norm not in {"story", "devotional", "prayer"}:
            kind_norm = "story"
        image_mode = (getattr(request, "image_mode", None) or "").strip().lower()
        if image_mode not in {"single", "multiple"}:
            image_mode = "single" if kind_norm == "prayer" else "multiple"
        try:
            requested_minutes = int(getattr(request, "duration", 5) or 5)
        except Exception:
            requested_minutes = 5
        requested_minutes = max(1, min(60, requested_minutes))
        default_voice_style = "soft_prayer" if kind_norm == "prayer" else "human"
        voice_style = (getattr(request, "voice_style", None) or default_voice_style).strip() or default_voice_style
        voice_gender = (getattr(request, "voice_gender", None) or "female").strip().lower() or "female"
        if voice_gender not in {"male", "female"}:
            voice_gender = "female"
        topic_display = request.topic if request.mode == 'topic' else ("Devocional" if kind_norm == "devotional" else ("Reflexão com Oração" if kind_norm == "prayer" else "História Personalizada"))
        update_task(task_id, status="processing", progress=5, message=f"Iniciando geração sobre: {topic_display}", result=_merged_task_result({
            "executor_id": executor_id,
            "executor_started_at": lease_info.get("started_at"),
            "executor_heartbeat_at": lease_info.get("heartbeat_at"),
            "attempt_number": int(lease_info.get("attempt_number") or 1),
            "pipeline_stage": "starting",
        }))
        runtime_monitor_stop, runtime_monitor_thread = _start_video_runtime_monitor(task_id, executor_id)
        print(f"Iniciando geração de vídeo ({request.mode}): {topic_display}")
        heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
        
        ai_service = AIContentGenerator()
        video_service = VideoGenerator(ai_service=ai_service)
        yt_service = YouTubeService()
        try:
            guardian_payload = request.model_dump()  # type: ignore[attr-defined]
        except Exception:
            guardian_payload = request.dict()
        guardian_db = SessionLocal()
        guardian_task_row = guardian_db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
        guardian_user_id = getattr(guardian_task_row, "user_id", None) if guardian_task_row else None
        current_task = get_task(task_id) or {}
        current_result = current_task.get("result") if isinstance(current_task.get("result"), dict) else {}
        source_module = "youtube_series" if isinstance(current_result.get("series_context"), dict) else None
        ai_service.set_operation_context(
            user_id=guardian_user_id,
            task_id=str(task_id),
            source_module=source_module,
        )
        guardian_context = youtube_auto_financial_adapter.build_context(
            task_id=str(task_id),
            payload=guardian_payload,
            user_id=guardian_user_id,
            status="queued",
        )
        guardian_preflight = financial_guardian_service.evaluate_context_preflight(
            guardian_db,
            context=guardian_context,
            config=youtube_auto_financial_adapter.build_guardrail_config(),
            adapter=youtube_auto_financial_adapter,
        )
        guardian_db.commit()
        update_task(task_id, result=_merged_task_result({
            "financial_guardian": {
                "source_type": "youtube_auto",
                "preflight": guardian_preflight,
            }
        }))
        if not guardian_preflight.get("allowed"):
            final_payload = _merged_task_result({
                "financial_guardian": {
                    "source_type": "youtube_auto",
                    "preflight": guardian_preflight,
                }
            })
            finalize_task_once(
                task_id,
                status="failed",
                progress=0,
                message=f"Guardiao financeiro bloqueou a geracao: {guardian_preflight.get('reason') or 'limites excedidos.'}",
                result=final_payload,
            )
            return

        # Checagem local, sem chamada paga: falha antes de roteiro/TTS quando a
        # chave, política, saldo conhecido ou circuit breaker impedem imagens.
        image_provider_preflight = ai_service.ai_router.ensure_image_provider_ready(
            user_id=guardian_user_id,
            task_id=str(task_id),
            capability=AICapability.IMAGE_GENERATION,
        )
        update_task(task_id, result=_merged_task_result({
            "image_provider_preflight": image_provider_preflight,
            "pipeline_stage": "provider_preflight_ok",
        }))
        
        # 1. Gerar Roteiro
        update_task(task_id, progress=10, message="1/8 Gerando roteiro com IA...", result=_merged_task_result({"pipeline_stage": "stage_1_script"}))
        heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
        _raise_if_cancelled()
        update_task(task_id, result=_merged_task_result({"script_staging": True}))
        
        def _count_words(txt: str) -> int:
            try:
                import re as _re
                return len(_re.findall(r"\w+", str(txt or ""), flags=_re.UNICODE))
            except Exception:
                return len(str(txt or "").split())

        def _build_story_plan_from_text(story_text: str, duration_minutes: int, kind: str) -> Dict[str, Any]:
            import re as _re
            t = str(story_text or "").strip()
            if not t:
                base_title = "Devocional" if kind == "devotional" else ("Reflexão com Oração" if kind == "prayer" else "História")
                return {"title": base_title, "description": "", "tags": [], "scenes": [{"text": "Conteúdo em preparação.", "image_prompt": "cinematic inspiring scene"}]}

            t = t.replace("\r\n", "\n").replace("\r", "\n")
            t = _re.sub(r"\n{3,}", "\n\n", t).strip()
            words_per_minute = 145
            if kind == "devotional":
                words_per_minute = 150
            elif kind == "prayer":
                words_per_minute = 155
            target_words = max(1, int(duration_minutes or 1)) * words_per_minute
            min_words = int(target_words * 0.95)
            for attempt in range(2):
                cur_words = _count_words(t)
                if cur_words >= min_words:
                    break
                try:
                    improved = ai_service.improve_story_or_devotional_text(
                        original_text=t,
                        instruction=(
                            f"Expanda o texto para atingir no mínimo {duration_minutes} minutos de narração, "
                            "sem resumir e sem repetir frases, ideias, versículos ou explicações já ditas. "
                            "Cada parágrafo deve acrescentar conteúdo novo e sustentar a narração até o fim."
                        ),
                        kind=kind,
                        duration_min_minutes=int(duration_minutes or 10),
                        duration_max_minutes=int(duration_minutes or 10),
                    )
                    if isinstance(improved, str) and improved.strip():
                        t = improved.strip()
                        continue
                except Exception:
                    pass
                break

            cur_words = _count_words(t)
            if cur_words < min_words:
                try:
                    safe_kind = "devocional" if kind == "devotional" else ("reflexão com oração" if kind == "prayer" else "história")
                    instr = (
                        f"Reescreva e EXPANDA este(a) {safe_kind} para ter pelo menos {duration_minutes} minutos de narração. "
                        "Mantenha o sentido, os personagens e a mensagem do texto base, mas aprofunde com detalhes, exemplos, aplicações e reflexões. "
                        "Nao repita frases, nao recapitule o que ja foi dito, nao enrole e nao use blocos redundantes. "
                        "Entregue um texto continuo, pronto para narracao, do inicio ao fim.\n\n"
                        f"TEXTO BASE:\n{t[:6000]}"
                    )
                    regenerated = ai_service.generate_story_or_devotional_text(
                        instruction=instr,
                        kind=kind,
                        duration_min_minutes=int(duration_minutes or 10),
                        duration_max_minutes=int(duration_minutes or 10),
                    )
                    if isinstance(regenerated, str) and regenerated.strip():
                        t = regenerated.strip()
                except Exception:
                    pass

            first_line = ""
            for ln in t.split("\n"):
                s = (ln or "").strip()
                if s:
                    first_line = s
                    break
            title_guess = first_line[:120] if first_line else ("Devocional" if kind == "devotional" else ("Reflexão com Oração" if kind == "prayer" else "História"))
            try:
                if hasattr(ai_service, "generate_strong_title_from_text"):
                    stronger = ai_service.generate_strong_title_from_text(t, kind=kind, max_len=80)
                    if isinstance(stronger, str) and stronger.strip():
                        title_guess = stronger.strip()[:120]
            except Exception:
                pass

            words_per_scene = 85
            max_words_per_scene = 140
            min_words_per_scene = 50
            sentences = _re.split(r"(?<=[.!?])\s+", t)
            scenes: List[Dict[str, Any]] = []
            buf_parts: List[str] = []
            buf_words = 0

            def _flush():
                nonlocal buf_parts, buf_words, scenes
                chunk = " ".join([p.strip() for p in buf_parts if p and p.strip()]).strip()
                buf_parts = []
                buf_words = 0
                if not chunk:
                    return
                chunk_words = _count_words(chunk)
                if chunk_words < 5:
                    return
                if kind == "prayer":
                    ip = (
                        f"Photorealistic cinematic Christian meditation scene inspired by this prayer reflection: {chunk[:180]}. "
                        "Soft angelic atmosphere, warm golden and white light, peaceful sacred ambience, gentle heavenly glow, serene prayerful mood, "
                        "family-friendly, natural anatomy, realistic humans, no horror, no dark mood, no grotesque details, no surreal abstraction."
                    )
                elif kind == "devotional":
                    ip = (
                        f"Photorealistic cinematic devotional Christian scene inspired by this message: {chunk[:180]}. "
                        "Reverent biblical atmosphere, hopeful expression, warm divine light, realistic humans, family-friendly, no horror."
                    )
                else:
                    ip = f"Photorealistic cinematic photography representing: {chunk[:160]}"
                scenes.append({"text": chunk, "image_prompt": ip})

            for s in sentences:
                st = (s or "").strip()
                if not st:
                    continue
                w = _count_words(st)
                if w > (max_words_per_scene + 40):
                    parts = _re.split(r"(?<=[,;:])\s+", st)
                else:
                    parts = [st]
                for part in parts:
                    p = (part or "").strip()
                    if not p:
                        continue
                    pw = _count_words(p)
                    if buf_words and (buf_words + pw) > max_words_per_scene:
                        _flush()
                    buf_parts.append(p)
                    buf_words += pw
                    if buf_words >= words_per_scene:
                        _flush()

            if buf_parts:
                _flush()

            merged: List[Dict[str, Any]] = []
            acc = None
            for sc in scenes:
                if acc is None:
                    acc = dict(sc)
                    continue
                if _count_words(acc.get("text") or "") < min_words_per_scene:
                    acc["text"] = (str(acc.get("text") or "") + " " + str(sc.get("text") or "")).strip()
                    if not (acc.get("image_prompt") or "").strip():
                        acc["image_prompt"] = sc.get("image_prompt") or ""
                else:
                    merged.append(acc)
                    acc = dict(sc)
            if acc is not None:
                merged.append(acc)
            scenes = merged

            hard_max_scenes = max(12, min(120, int(duration_minutes or 10) * 4))
            if len(scenes) > hard_max_scenes:
                scenes = scenes[:hard_max_scenes]

            tags = ["reflexão", "fé", "motivação"]
            music_mood = "emotional_cinematic"
            music_prompt = None
            music_mood_fallback = None
            bg_music_volume = None
            allow_image_reuse = False
            prefer_peaceful_music = False
            single_bg = False
            background_prompt = None
            if kind == "devotional":
                tags = ["devocional", "bíblia", "fé", "oração", "reflexão"]
                music_mood = "happy"
            elif kind == "prayer":
                tags = ["oração", "reflexão", "meditação", "paz", "fé", "tranquilidade"]
                music_mood = "happy"
                music_mood_fallback = "happy"
                music_prompt = "soft prayer meditation ambient, gentle piano, airy pads, peaceful worship background, calm, serene, relaxing, no percussion aggression"
                bg_music_volume = 0.025
                allow_image_reuse = True
                prefer_peaceful_music = True
                single_bg = image_mode == "single"
                background_prompt = (
                    "Photorealistic cinematic Christian prayer meditation scene, soft angelic atmosphere, warm golden and white light, "
                    "peaceful heavenly ambience, serene sacred setting, family-friendly, realistic humans, no horror, no grotesque details, "
                    "no abstract collage, no text, no watermark."
                )
            else:
                tags = ["história", "reflexão", "fé", "motivação"]

            plan: Dict[str, Any] = {
                "title": title_guess,
                "description": (t[:1200] + ("..." if len(t) > 1200 else "")).strip(),
                "tags": tags,
                "scenes": scenes,
                "music_mood": music_mood,
                "kind": kind,
            }
            if music_prompt:
                plan["music_prompt"] = music_prompt
            if music_mood_fallback:
                plan["music_mood_fallback"] = music_mood_fallback
            if bg_music_volume is not None:
                plan["bg_music_volume"] = bg_music_volume
            if allow_image_reuse:
                plan["allow_image_reuse"] = True
            if prefer_peaceful_music:
                plan["prefer_peaceful_music"] = True
            if single_bg:
                plan["single_bg"] = True
            if background_prompt:
                plan["background_prompt"] = background_prompt
            return plan

        script = dict(request.seeded_script) if isinstance(request.seeded_script, dict) else None
        seed_mode = bool(getattr(request, "force_render_only", False) or getattr(request, "force_reuse_assets", False))
        if seed_mode and guardian_task_row is not None:
            try:
                seed_result = json.loads(getattr(guardian_task_row, "result_json", "") or "{}")
            except Exception:
                seed_result = {}
            if isinstance(seed_result, dict):
                seed_script = seed_result.get("script") if isinstance(seed_result.get("script"), dict) else None
                seed_render_report = seed_result.get("render_report") if isinstance(seed_result.get("render_report"), dict) else {}
                seed_audio_path = ""
                try:
                    seed_audio_path = str(((seed_render_report.get("audio_generation") or {}).get("output_path") or "")).strip()
                except Exception:
                    seed_audio_path = ""
                seed_narration_text = ""
                try:
                    seed_narration_text = str(((seed_render_report.get("audio_generation") or {}).get("final_text_sent_to_tts") or "")).strip()
                except Exception:
                    seed_narration_text = ""
                if not seed_narration_text:
                    try:
                        seed_narration_text = str(((seed_render_report.get("narration_plan") or {}).get("full_text") or "")).strip()
                    except Exception:
                        seed_narration_text = ""
                seed_selected_images: List[str] = []
                if isinstance(seed_script, dict) and isinstance(seed_script.get("selected_images"), list):
                    seed_selected_images = [
                        str(x).strip()
                        for x in seed_script.get("selected_images")
                        if isinstance(x, str) and str(x).strip()
                    ]

                seed_script_ok = _is_valid_seed_script(seed_script)
                seed_audio_ok = _file_ok(seed_audio_path)
                seed_images_ok = _selected_images_ok(seed_selected_images)

                if bool(getattr(request, "force_render_only", False)):
                    if not (seed_script_ok and seed_images_ok and seed_audio_ok):
                        finalize_task_once(
                            task_id,
                            status="failed",
                            progress=0,
                            message="Recuperação (render-only) bloqueada: faltam roteiro, imagens ou áudio válidos para reutilização.",
                            result=_merged_task_result({
                                "recovery": {
                                    "mode": "render_only",
                                    "seed_script_ok": bool(seed_script_ok),
                                    "seed_images_ok": bool(seed_images_ok),
                                    "seed_audio_ok": bool(seed_audio_ok),
                                }
                            }),
                        )
                        return
                    script = dict(seed_script or {})
                    script["selected_images"] = seed_selected_images
                    script["seed_audio_path"] = seed_audio_path
                    if seed_narration_text:
                        script["seed_narration_text"] = seed_narration_text
                    script["force_reuse_assets"] = True
                    script["force_render_only"] = True
                    update_task(task_id, progress=10, message="1/8 Recuperação (render-only): reutilizando roteiro/imagens/áudio e renderizando MP4...", result=_merged_task_result({"pipeline_stage": "stage_1_script_recovery"}))
                elif bool(getattr(request, "force_reuse_assets", False)) and seed_script_ok:
                    script = dict(seed_script or {})
                    reused: List[str] = ["roteiro"]
                    if seed_images_ok:
                        script["selected_images"] = seed_selected_images
                        reused.append("imagens")
                    if seed_audio_ok:
                        script["seed_audio_path"] = seed_audio_path
                        if seed_narration_text:
                            script["seed_narration_text"] = seed_narration_text
                        reused.append("áudio")
                    script["force_reuse_assets"] = True
                    update_task(task_id, progress=10, message=f"1/8 Recuperação: reutilizando {', '.join(reused)} e gerando apenas o que faltar...", result=_merged_task_result({"pipeline_stage": "stage_1_assets_recovery"}))

        if script is None:
            if request.mode == 'story' and request.story_content:
                minutes = 10
                try:
                    minutes = requested_minutes
                except Exception:
                    minutes = 10
                minutes = max(1, min(60, minutes))
                script = _build_story_plan_from_text(request.story_content, minutes, kind_norm)
                if isinstance(script, dict) and bool(getattr(request, 'editorial_reviewed', False)):
                    script['editorial_reviewed'] = True
                    script['editorial_review_ready'] = bool(getattr(request, 'editorial_review_ready', False))
            else:
                topic = request.topic or "Motivação Genérica"
                script = ai_service.generate_motivational_script(topic, requested_minutes)
            
        print("Roteiro gerado/estruturado.")
        _raise_if_cancelled()

        if isinstance(script, dict):
            def _target_scene_count(duration_minutes: int) -> int:
                try:
                    m = int(duration_minutes or 1)
                except Exception:
                    m = 1
                m = max(1, m)
                try:
                    spm_raw = (os.getenv("YOUTUBE_SCENES_PER_MINUTE") or "").strip()
                    spm = float(spm_raw) if spm_raw else 1.35
                except Exception:
                    spm = 1.35
                spm = max(0.8, min(2.4, spm))
                try:
                    min_raw = (os.getenv("YOUTUBE_SCENES_MIN") or "").strip()
                    min_scenes = int(min_raw) if min_raw else 8
                except Exception:
                    min_scenes = 8
                try:
                    max_raw = (os.getenv("YOUTUBE_SCENES_MAX") or "").strip()
                    max_scenes = int(max_raw) if max_raw else 15
                except Exception:
                    max_scenes = 15
                min_scenes = max(4, min(15, min_scenes))
                max_scenes = max(min_scenes, min(18, max_scenes))
                return max(min_scenes, min(max_scenes, int(round(m * spm))))

            def _compact_scenes(raw: Any, target_count: int) -> List[Dict[str, Any]]:
                scenes_in: List[Dict[str, Any]] = []
                if isinstance(raw, list):
                    for s in raw:
                        if isinstance(s, str):
                            txt = s.strip()
                            if txt:
                                scenes_in.append({"text": txt, "image_prompt": ""})
                            continue
                        if not isinstance(s, dict):
                            continue
                        txt = (s.get("text") or s.get("narration") or s.get("narration_text") or s.get("script") or "").strip()
                        if not txt:
                            continue
                        ip = (s.get("image_prompt") or s.get("visual_prompt") or s.get("prompt") or "").strip()
                        cap = (s.get("caption") or s.get("on_screen_text") or "").strip()
                        out: Dict[str, Any] = {"text": txt, "image_prompt": ip}
                        if cap:
                            out["caption"] = cap
                        scenes_in.append(out)

                if not scenes_in:
                    return []
                if target_count <= 0 or len(scenes_in) <= target_count:
                    return scenes_in

                group_size = max(1, int(math.ceil(float(len(scenes_in)) / float(target_count))))
                merged: List[Dict[str, Any]] = []
                for i in range(0, len(scenes_in), group_size):
                    group = scenes_in[i:i + group_size]
                    if not group:
                        continue
                    txt = " ".join((g.get("text") or "").strip() for g in group).strip()
                    ip = ""
                    cap = ""
                    for g in group:
                        if not ip:
                            ip = (g.get("image_prompt") or "").strip()
                        if not cap:
                            cap = (g.get("caption") or "").strip()
                        if ip and cap:
                            break
                    out: Dict[str, Any] = {"text": txt, "image_prompt": ip}
                    if cap:
                        out["caption"] = cap
                    if out.get("text"):
                        merged.append(out)

                while len(merged) > target_count:
                    a = merged.pop()
                    b = merged.pop()
                    joined = f"{(b.get('text') or '').strip()} {(a.get('text') or '').strip()}".strip()
                    ip = (b.get("image_prompt") or "").strip() or (a.get("image_prompt") or "").strip()
                    cap = (b.get("caption") or "").strip() or (a.get("caption") or "").strip()
                    out: Dict[str, Any] = {"text": joined, "image_prompt": ip}
                    if cap:
                        out["caption"] = cap
                    merged.append(out)

                return merged

            try:
                desc = (script.get("description") or "").strip()
                if not desc and request.story_content:
                    dprompt = (
                        "Crie uma descrição otimizada para YouTube (6-10 linhas) com base nesta mensagem. "
                        "Inclua CTA e 5-10 hashtags relevantes.\n\n"
                        f"MENSAGEM:\n{(request.story_content or '').strip()[:4000]}"
                    )
                    gen_desc = (ai_service._generate_text(dprompt, system_prompt="Você é um copywriter de YouTube. Retorne apenas o texto.", json_mode=False) or "").strip()
                    if gen_desc:
                        script["description"] = gen_desc[:2000]
            except Exception:
                pass

            try:
                if request.override_title and str(request.override_title).strip():
                    script["title"] = str(request.override_title).strip()[:120]
                if request.override_description and str(request.override_description).strip():
                    script["description"] = str(request.override_description).strip()[:4000]
                if request.override_tags and isinstance(request.override_tags, list):
                    tags = [str(t).strip() for t in request.override_tags if isinstance(t, (str, int, float)) and str(t).strip()]
                    if tags:
                        script["tags"] = tags[:30]
            except Exception:
                pass

            try:
                target = _target_scene_count(requested_minutes)
                script["disable_scene_text_split"] = True
                raw_scenes = script.get("scenes")
                compacted = _compact_scenes(raw_scenes, target)
                if compacted:
                    script["scenes"] = compacted
            except Exception:
                pass

            try:
                script = ai_service.build_cinematic_engine_v2_plan(
                    script,
                    target_scene_count=_target_scene_count(requested_minutes),
                    min_scene_count=8,
                    max_scene_count=15,
                )
            except Exception as e:
                print(f"Aviso: Codexia Cinematic Engine V2 nao conseguiu enriquecer o storyboard: {e}")

            script["target_duration_sec"] = int(requested_minutes * 60)
            script["target_duration_min"] = int(requested_minutes)
            script["kind"] = kind_norm
            if kind_norm == "prayer" and image_mode == "single":
                script["single_bg"] = True

            selected = []
            if request.selected_images and isinstance(request.selected_images, list):
                for v in request.selected_images:
                    if isinstance(v, str) and v.strip():
                        selected.append(v.strip())
            if request.custom_image_paths and isinstance(request.custom_image_paths, list):
                for v in request.custom_image_paths:
                    if isinstance(v, str) and v.strip():
                        selected.append(v.strip())
            if selected:
                script["selected_images"] = selected[:24]
            if guardian_db is not None:
                guardian_context = youtube_auto_financial_adapter.build_context(
                    task_id=str(task_id),
                    payload=guardian_payload,
                    script=script,
                    user_id=guardian_user_id,
                    status="processing",
                )
                script = financial_guardian_service.hydrate_plan_with_cached_images_for_context(
                    guardian_db,
                    context=guardian_context,
                    plan=script,
                )
                financial_guardian_service.record_context_event(
                    guardian_db,
                    context=guardian_context,
                    event_type="production_started",
                    stage="storyboard_ready",
                    estimated_cost=guardian_context.estimated_cost,
                    actual_cost=guardian_context.actual_cost,
                    details={
                        "topic_display": topic_display,
                        "auto_upload": bool(request.auto_upload),
                        "scene_count": len(script.get("scenes") or []) if isinstance(script.get("scenes"), list) else 0,
                    },
                )
                guardian_db.commit()

            update_task(task_id, progress=13, message="1/8 Aplicando revisão editorial no roteiro...", result=_merged_task_result({"pipeline_stage": "stage_1_editorial"}))
            heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
            _raise_if_cancelled()
            script = _apply_youtube_auto_editorial_intelligence(
                guardian_db,
                script,
                ai_service=ai_service,
                task_id=str(task_id),
            )
            update_task(task_id, result=_merged_task_result({
                "editorial_intelligence": script.get("editorial_intelligence") if isinstance(script, dict) else {},
                "script": script if isinstance(script, dict) else None,
            }))
        
        # 2. Gerar Vídeo (16:9)
        # Passamos uma função de callback para atualizar o progresso com as 8 etapas
        #   20-34% → 2/8 Gerando narração
        #   35-49% → 3/8 Preparando imagens
        #   50-64% → 4/8 Sincronizando cenas
        #   65-74% → 5/8 Gerando legendas
        #   75-89% → 6/8 Renderizando
        #   90-97% → 7/8 Validando
        #   98-100% → 8/8 Concluído
        render_output = {"filename": None}
        def _stage_for_pct(pct: int) -> Tuple[int, str, str]:
            # pct: percentual do create_video_from_plan (0..100)
            if pct < 20:
                # Geração de áudio inicial (TTS) → etapa 2
                return (20 + int(pct * 0.15 / 20 + 1e-6), "2/8 Gerando narração com IA...", "stage_2_voice")
            if pct < 40:
                sub = int((pct - 20) / 20 * 14)  # 20-34%
                return (20 + sub, "2/8 Gerando narração com IA...", "stage_2_voice")
            if pct < 55:
                sub = int((pct - 40) / 15 * 15)  # 35-49%
                return (35 + sub, "3/8 Preparando imagens e cenas...", "stage_3_images")
            if pct < 70:
                sub = int((pct - 55) / 15 * 15)  # 50-64%
                return (50 + sub, "4/8 Sincronizando cenas com narração...", "stage_4_sync")
            if pct < 82:
                sub = int((pct - 70) / 12 * 10)   # 65-74%
                return (65 + sub, "5/8 Gerando legendas sincronizadas...", "stage_5_captions")
            if pct < 95:
                sub = int((pct - 82) / 13 * 15)   # 75-89%
                return (75 + sub, "6/8 Renderizando vídeo final...", "stage_6_render")
            return (89, "6/8 Renderizando vídeo final...", "stage_6_render")

        def progress_callback(progress, message):
            raw = 0
            try:
                raw = int(progress or 0)
            except Exception:
                raw = 0
            raw = max(0, min(100, raw))
            task_pct, stage_msg, stage_key = _stage_for_pct(raw)
            detail = str(message or "").strip()
            detail = re.sub(r"\s*output=[^\s]+", "", detail).strip()
            if detail and detail.lower().rstrip(".") != stage_msg.lower().rstrip("."):
                visible_message = f"{stage_msg.rstrip('.')} — {detail}"
            else:
                visible_message = stage_msg
            update_task(
                task_id,
                progress=task_pct,
                message=visible_message[:500],
                result=_merged_task_result({"pipeline_stage": stage_key, "stage_detail": detail[:300]}),
            )
            try:
                msg_txt = str(message or "")
                if ("output=" in msg_txt) and (not render_output.get("filename")):
                    candidate = msg_txt.split("output=", 1)[1].strip().split()[0].strip()
                    if candidate.endswith(".mp4"):
                        render_output["filename"] = candidate
                        update_task(task_id, result=_merged_task_result({
                            "rendering": {
                                "output_filename": candidate,
                                "output_url": f"/static/videos/{candidate}",
                            }
                        }))
            except Exception:
                pass
            heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
            _raise_if_cancelled()
            
        video_result = video_service.create_video_from_plan(
            script,
            aspect_ratio=str(request.aspect_ratio or "16:9"),
            progress_callback=progress_callback,
            voice_style=voice_style,
            voice_gender=voice_gender,
            music_file_path=(str(request.music_file_path).strip() if request.music_file_path else None),
        )
        _dbg_event("H1", "create_video_from_plan returned", {
            "task_id": str(task_id),
            "executor_id": executor_id,
            "video_result_keys": sorted(list(video_result.keys())) if isinstance(video_result, dict) else None,
            "video_url": (video_result.get("video_url") if isinstance(video_result, dict) else None),
        })
        video_path = video_result["video_url"]
        render_report = video_result.get("render_report") if isinstance(video_result, dict) else {}
        if not isinstance(render_report, dict):
            render_report = {}
        sync_validation = render_report.get("sync_validation")
        audio_generation = render_report.get("audio_generation")
        # ====== ETAPA 7/8: VALIDAÇÕES OBRIGATÓRIAS ======
        # Regra: qualquer falha → status=failed → NÃO coloca em Aguardando Publicação
        update_task(task_id, progress=92, message="7/8 Validando arquivo de vídeo final...", result=_merged_task_result({"pipeline_stage": "stage_7_validation"}))
        heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
        abs_video_path = _resolve_rendered_video_file_path(video_result)
        validation: Dict[str, Any] = {"ok": False, "checks": {}}
        try:
            _vsz = 0
            if abs_video_path and os.path.exists(abs_video_path):
                _vsz = os.path.getsize(abs_video_path)
            validation["checks"]["file_exists"] = bool(abs_video_path and os.path.exists(abs_video_path))
            validation["checks"]["size_gt_100kb"] = int(_vsz or 0) > 100 * 1024
            validation["file_size_bytes"] = int(_vsz or 0)
            _ffv = probe_media_file(abs_video_path) if abs_video_path else {}
            validation["checks"]["ffprobe_available"] = bool((_ffv or {}).get("probe_available"))
            validation["checks"]["video_stream"] = bool((_ffv or {}).get("video_stream"))
            validation["checks"]["audio_stream"] = bool((_ffv or {}).get("audio_stream"))
            _vdur = float((_ffv or {}).get("video_duration") or 0.0)
            _adur = float((_ffv or {}).get("audio_duration") or 0.0)
            validation["video_duration_sec"] = round(float(_vdur or 0), 3)
            validation["audio_duration_sec"] = round(float(_adur or 0), 3)
            validation["checks"]["duration_valid"] = (float(_vdur or 0) > 0.5)
            validation["checks"]["audio_not_trimmed"] = media_durations_match(_ffv or {})
            if (_ffv or {}).get("error"):
                validation["probe_error"] = str((_ffv or {}).get("error"))[:240]
            # VIDEO_URL_PREFIX e VIDEO_OUTPUT_DIR são definidos em conjunto no
            # config; se o arquivo final resolvido existe e passou pelo tamanho
            # mínimo, a rota /static ou /media correspondente está pronta.
            _http_ready = validation["checks"]["file_exists"] and validation["checks"]["size_gt_100kb"]
            validation["checks"]["http_media_ready"] = bool(_http_ready)
            all_checks_ok = all(bool(v) for v in validation["checks"].values()) if validation["checks"] else False
            validation["ok"] = bool(all_checks_ok)
        except Exception as _e:
            validation["error"] = f"{type(_e).__name__}: {str(_e)[:200]}"
            validation["ok"] = False
        update_task(task_id, progress=96, message="7/8 Validação concluída.", result=_merged_task_result({"final_validation": validation, "pipeline_stage": "stage_7_validation_done"}))
        if not validation.get("ok"):
            # Tarefa FAILED → NÃO vai para Aguardando Publicação
            detail_items = [f"{k}={validation['checks'].get(k)}" for k in sorted(validation.get("checks", {}).keys())]
            finalize_task_once(
                task_id,
                status="failed",
                progress=0,
                message=(
                    f"Validação final do MP4 reprovada ({'; '.join(detail_items)[:300]}). "
                    "Vídeo NÃO foi colocado em Aguardando Publicação para evitar publicação quebrada."
                ),
                result=_merged_task_result({
                    "video_url": video_path,
                    "file_path": abs_video_path,
                    "title": (script.get("title") if isinstance(script, dict) else None),
                    "kind": "story" if request.mode == "story" else "topic",
                    "final_validation": validation,
                    "render_report": render_report if isinstance(render_report, dict) else None,
                    "audio_generation": audio_generation,
                    "sync_validation": sync_validation,
                    "executor_id": executor_id,
                    "attempt_number": int(lease_info.get("attempt_number") or 1),
                }),
            )
            return
        update_task(task_id, result=_merged_task_result({
            "render_report": render_report,
            "script": script if isinstance(script, dict) else None,
        }))
        if guardian_db is not None and guardian_context is not None:
            scene_visuals = render_report.get("scene_visuals") if isinstance(render_report.get("scene_visuals"), list) else []
            generated_image_paths = [
                str(item.get("image_path") or "").strip()
                for item in scene_visuals
                if isinstance(item, dict) and str(item.get("image_path") or "").strip()
            ]
            guardian_context = youtube_auto_financial_adapter.build_context(
                task_id=str(task_id),
                payload=guardian_payload,
                script=script if isinstance(script, dict) else None,
                video_result=video_result if isinstance(video_result, dict) else None,
                user_id=guardian_user_id,
                status="rendered",
            )
            if generated_image_paths and isinstance(script, dict):
                guardian_cache_summary = financial_guardian_service.cache_images_from_context_result(
                    guardian_db,
                    context=guardian_context,
                    plan=script,
                    image_paths=generated_image_paths,
                )
            guardian_db.commit()
        heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
        _raise_if_cancelled()
        
        # Path absoluto para upload (compatível com Docker e /data/media)
        from app.config import absolute_path_for_video
        abs_video_path = absolute_path_for_video(video_path)
        print(f"Vídeo gerado em: {abs_video_path}")
        
        # 3. Upload (se solicitado)
        if request.auto_upload:
            update_task(task_id, progress=90, message="8/8 Concluído. Iniciando upload automático para o YouTube...", result=_merged_task_result({"pipeline_stage": "stage_8_completed"}))
            heartbeat_task_execution_lease(task_id, executor_id, ttl_seconds=5 * 60)
            print("Iniciando upload para YouTube...")
            
            description = script.get('description', 'Vídeo motivacional.')
            if video_result.get("music_credit"):
                description += f"\n\n{video_result['music_credit']}"

            fallback_title = (request.topic or "Vídeo").strip()
            
            upload_result = yt_service.upload_video(
                abs_video_path,
                title=script.get('title') or fallback_title,
                description=description,
                tags=script.get('tags', ['motivação', 'sucesso']),
                thumbnail_path=(request.thumbnail_path or None),
            )
            youtube_video_id = _extract_uploaded_youtube_id(upload_result)
            youtube_url = _build_youtube_watch_url(youtube_video_id)
            upload_payload = _serialize_upload_result(upload_result)
            upload_status = "completed" if youtube_video_id else "failed"
            guardian_summary = None
            if guardian_db is not None and guardian_context is not None:
                guardian_context = youtube_auto_financial_adapter.build_context(
                    task_id=str(task_id),
                    payload=guardian_payload,
                    script=script if isinstance(script, dict) else None,
                    video_result=video_result if isinstance(video_result, dict) else None,
                    user_id=guardian_user_id,
                    status="completed" if youtube_video_id else "rendered_upload_failed",
                )
                guardian_summary = {
                    "source_type": "youtube_auto",
                    "preflight": guardian_preflight,
                    "cache_summary": guardian_cache_summary,
                    "estimated_cost": guardian_context.estimated_cost,
                    "actual_cost": guardian_context.actual_cost,
                    "estimated_savings": round(max(0.0, guardian_context.estimated_cost - guardian_context.actual_cost), 4),
                }
                financial_guardian_service.record_context_event(
                    guardian_db,
                    context=guardian_context,
                    event_type="production_completed" if youtube_video_id else "production_failed",
                    stage="upload_completed" if youtube_video_id else "upload_failed",
                    estimated_cost=guardian_context.estimated_cost,
                    actual_cost=guardian_context.actual_cost,
                    details={
                        "cache_summary": guardian_cache_summary,
                        "upload_result": upload_payload,
                        "estimated_savings": guardian_summary["estimated_savings"],
                    },
                )
                guardian_db.commit()
            if not youtube_video_id:
                err_msg = _publish_error_message(upload_result, action_label="publicar")
                final_payload = _merged_task_result({
                    "video_url": video_path,
                    "file_path": abs_video_path,
                    "title": script.get("title"),
                    "description": description,
                    "tags": script.get("tags"),
                    "kind": "story" if request.mode == "story" else "topic",
                    "editorial_intelligence": script.get("editorial_intelligence") if isinstance(script, dict) else {},
                    "script": script if isinstance(script, dict) else None,
                    "render_report": render_report if isinstance(render_report, dict) else None,
                    "audio_generation": audio_generation,
                    "sync_validation": sync_validation,
                    "executor_id": executor_id,
                    "attempt_number": int(lease_info.get("attempt_number") or 1),
                    "financial_guardian": guardian_summary,
                    "upload_status": upload_status,
                    "upload_result": upload_payload,
                    "youtube_video_id": None,
                    "youtube_url": None,
                })
                finalize_task_once(
                    task_id,
                    status="rendered_upload_failed",
                    progress=100,
                    message=f"Video renderizado com sucesso, mas o upload falhou: {err_msg}",
                    result=final_payload,
                )
                return
            final_payload = _merged_task_result({
                "video_url": video_path,
                "file_path": abs_video_path,
                "title": script.get("title"),
                "description": description,
                "tags": script.get("tags"),
                "kind": "story" if request.mode == "story" else "topic",
                "editorial_intelligence": script.get("editorial_intelligence") if isinstance(script, dict) else {},
                "script": script if isinstance(script, dict) else None,
                "render_report": render_report if isinstance(render_report, dict) else None,
                "audio_generation": audio_generation,
                "sync_validation": sync_validation,
                "executor_id": executor_id,
                "attempt_number": int(lease_info.get("attempt_number") or 1),
                "financial_guardian": guardian_summary,
                "upload_status": upload_status,
                "upload_result": upload_payload,
                "youtube_video_id": youtube_video_id,
                "youtube_url": youtube_url,
            })
            finalized = finalize_task_once(task_id, status="completed", progress=100, message="Vídeo gerado e publicado com sucesso!", result=final_payload)
            if finalized.get("finalized_now"):
                try:
                    dbn = SessionLocal()
                    n = SystemNotification(
                        user_id=None,
                        kind="video_generated",
                        title="Vídeo gerado",
                        message="Vídeo gerado e publicado com sucesso!",
                        payload_json=json.dumps({"task_id": task_id, "video_url": video_path}, ensure_ascii=False),
                        status="new",
                    )
                    dbn.add(n)
                    dbn.commit()
                except Exception:
                    try:
                        dbn.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        dbn.close()
                    except Exception:
                        pass
        else:
            guardian_summary = None
            if guardian_db is not None and guardian_context is not None:
                guardian_context = youtube_auto_financial_adapter.build_context(
                    task_id=str(task_id),
                    payload=guardian_payload,
                    script=script if isinstance(script, dict) else None,
                    video_result=video_result if isinstance(video_result, dict) else None,
                    user_id=guardian_user_id,
                    status="completed",
                )
                guardian_summary = {
                    "source_type": "youtube_auto",
                    "preflight": guardian_preflight,
                    "cache_summary": guardian_cache_summary,
                    "estimated_cost": guardian_context.estimated_cost,
                    "actual_cost": guardian_context.actual_cost,
                    "estimated_savings": round(max(0.0, guardian_context.estimated_cost - guardian_context.actual_cost), 4),
                }
                financial_guardian_service.record_context_event(
                    guardian_db,
                    context=guardian_context,
                    event_type="production_completed",
                    stage="render_completed",
                    estimated_cost=guardian_context.estimated_cost,
                    actual_cost=guardian_context.actual_cost,
                    details={
                        "cache_summary": guardian_cache_summary,
                        "estimated_savings": guardian_summary["estimated_savings"],
                    },
                )
                guardian_db.commit()
            final_payload = _merged_task_result({
                "video_url": video_path,
                "file_path": abs_video_path,
                "title": script.get("title"),
                "description": script.get("description"),
                "tags": script.get("tags"),
                "kind": "story" if request.mode == "story" else "topic",
                "editorial_intelligence": script.get("editorial_intelligence") if isinstance(script, dict) else {},
                "script": script if isinstance(script, dict) else None,
                "render_report": render_report if isinstance(render_report, dict) else None,
                "audio_generation": audio_generation,
                "sync_validation": sync_validation,
                "executor_id": executor_id,
                "attempt_number": int(lease_info.get("attempt_number") or 1),
                "financial_guardian": guardian_summary,
            })
            _dbg_event("H2", "before finalize_task_once(completed)", {
                "task_id": str(task_id),
                "executor_id": executor_id,
                "video_url": str(video_path),
                "progress": 100,
            })
            # --- UnifiedVideoPipeline: validação CENTRAL antes de liberar awaiting_review/completed ---
            _pipeline_validation = None
            _pipeline_status_target = "completed"
            _pipeline_final_message = "Vídeo gerado com sucesso!"
            _pipeline_final_status = "completed"
            try:
                if _unified_enabled() and unified_video_pipeline is not None:
                    from app.database import SessionLocal as _SLocal
                    _pdb = _SLocal()
                    try:
                        unified_video_pipeline().transition_status(
                            _pdb,
                            str(task_id),
                            status="validating",
                            step="validating",
                            progress=96,
                            message="Validando artefatos físicos / mp4 / áudio / imagens (UnifiedVideoPipeline)...",
                            merge_result=final_payload,
                        )
                        _pipeline_validation, _uv = unified_video_pipeline().transition_to_awaiting_review_if_valid(
                            _pdb,
                            str(task_id),
                            probe_local_paths=True,
                            probe_http=False,
                        )
                        if _pipeline_validation and not _pipeline_validation.ok and _uv is not None:
                            _pipeline_status_target = "failed"
                            _pipeline_final_status = "failed"
                            failed_check = str(_pipeline_validation.first_failed or "unknown")
                            _pipeline_final_message = (
                                "Validação pré-revisão falhou: "
                                f"{failed_check} — detalhes: {_json_dump_short(_pipeline_validation.details)}. "
                                "Corrija a etapa correspondente e gere novamente."
                            )
                            final_payload["unified_pipeline"] = {
                                "validation_checks": _pipeline_validation.checks,
                                "validation_first_failed": failed_check,
                                "status": "failed",
                            }
                        elif _pipeline_validation and _pipeline_validation.ok and _uv is not None:
                            # Se não precisa de revisão ou auto_upload=True, completed. Caso contrário: awaiting_review
                            _target_db_status = str(getattr(_uv, "status") or "awaiting_review")
                            if _target_db_status == "approved":
                                _pipeline_final_status = "completed"
                                _pipeline_status_target = "completed"
                                _pipeline_final_message = "Vídeo gerado, aprovado automaticamente e pronto para publicação (UnifiedVideoPipeline)."
                            elif _target_db_status == "published":
                                _pipeline_final_status = "completed"
                                _pipeline_status_target = "completed"
                            else:
                                # awaiting_review na UnifiedVideo → ainda marcamos a VideoTask como awaiting_review e finalizamos.
                                # Mantemos "completed" na task_manager (compatibilidade UI GET /task/{id} exibe bandejas);
                                # mas marcamos a task como "awaiting_review" também.
                                _pipeline_final_status = "awaiting_review"
                                _pipeline_status_target = "awaiting_review"
                                _pipeline_final_message = (
                                    "Vídeo gerado e validação física aprovada (UnifiedVideoPipeline): em Aguardando Revisão."
                                )
                            final_payload["unified_pipeline"] = {
                                "validation_checks": _pipeline_validation.checks,
                                "validation_ok": True,
                                "status": _target_db_status,
                                "unified_video_id": getattr(_uv, "id", None),
                            }
                        _pdb.commit()
                    finally:
                        try:
                            _pdb.close()
                        except Exception:
                            pass
            except Exception as _pexc:
                import traceback
                traceback.print_exc()
                final_payload.setdefault("unified_pipeline", {})
                final_payload["unified_pipeline"]["validation_error"] = f"{type(_pexc).__name__}: {str(_pexc)[:300]}"

            finalized = finalize_task_once(task_id, status=_pipeline_final_status, progress=100, message=_pipeline_final_message, result=final_payload)
            _dbg_event("H2", "after finalize_task_once(completed)", {
                "task_id": str(task_id),
                "executor_id": executor_id,
                "finalized": finalized,
            })
            if finalized.get("finalized_now"):
                try:
                    dbn = SessionLocal()
                    n = SystemNotification(
                        user_id=None,
                        kind="video_generated",
                        title="Vídeo gerado",
                        message="Vídeo gerado com sucesso!",
                        payload_json=json.dumps({"task_id": task_id, "video_url": video_path}, ensure_ascii=False),
                        status="new",
                    )
                    dbn.add(n)
                    dbn.commit()
                except Exception:
                    try:
                        dbn.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        dbn.close()
                    except Exception:
                        pass
            
    except _TaskPaused:
        try:
            t = get_task(task_id) or {}
            try:
                current_progress = int(t.get("progress") or 0)
            except Exception:
                current_progress = 0
            mark_task_paused(
                task_id,
                message=(
                    f"Produção pausada com segurança em {current_progress}%. "
                    "Roteiro, imagens, áudio e demais ativos já concluídos foram preservados."
                ),
            )
            _dbg_event("H3", "task paused", {"task_id": str(task_id), "executor_id": executor_id, "progress": current_progress})
        except Exception:
            pass
    except _TaskCancelled:
        try:
            t = get_task(task_id) or {}
            try:
                current_progress = int(t.get("progress") or 0)
            except Exception:
                current_progress = 0
            update_task(task_id, status="cancelled", progress=current_progress, message="Cancelado pelo usuário.")
            _dbg_event("H3", "task cancelled", {"task_id": str(task_id), "executor_id": executor_id, "progress": current_progress})
        except Exception:
            pass
    except Exception as e:
        print(f"Erro na tarefa {task_id}: {e}")
        try:
            import traceback as _tb
            _dbg_event("H3", "process_video_generation exception", {
                "task_id": str(task_id),
                "executor_id": executor_id,
                "error": str(e),
                "traceback": _tb.format_exc()[-4000:],
            })
        except Exception:
            pass
        if guardian_context is not None:
            try:
                if guardian_db is None:
                    guardian_db = SessionLocal()
                financial_guardian_service.record_context_event(
                    guardian_db,
                    context=guardian_context,
                    event_type="production_failed",
                    stage="execution",
                    severity="warning",
                    estimated_cost=getattr(guardian_context, "estimated_cost", 0.0),
                    actual_cost=getattr(guardian_context, "actual_cost", 0.0),
                    details={"error": str(e)[:500]},
                )
                guardian_db.commit()
            except Exception:
                try:
                    guardian_db.rollback()
                except Exception:
                    pass
        provider_error = e.to_dict() if isinstance(e, AIOperationBlocked) else None
        failure_result: Dict[str, Any] = {"pipeline_stage": "provider_error" if provider_error else "failed"}
        if provider_error:
            failure_result["provider_error"] = provider_error
        current_task = get_task(task_id) or {}
        try:
            current_progress = int(current_task.get("progress") or 0)
        except Exception:
            current_progress = 0
        update_task(
            task_id,
            status="failed",
            progress=current_progress,
            message=str(e),
            result=_merged_task_result(failure_result),
        )
    finally:
        if runtime_monitor_stop is not None:
            try:
                runtime_monitor_stop.set()
            except Exception:
                pass
        if runtime_monitor_thread is not None:
            try:
                runtime_monitor_thread.join(timeout=1.0)
            except Exception:
                pass
        if guardian_db is not None:
            try:
                guardian_db.close()
            except Exception:
                pass
        if executor_id:
            release_task_execution_lease(task_id, executor_id)
            _dbg_event("H5", "lease released", {"task_id": str(task_id), "executor_id": executor_id})
        if redis_lock:
            try:
                redis_lock.release()
            except Exception:
                pass
        if file_lock:
            try:
                file_lock.release()
            except Exception:
                pass
        try:
            _kick_story_video_task_queue_async()
        except Exception:
            pass


# ─── Community: All Comments (across videos) ────────────
@router.get("/community/all-comments")
def get_all_community_comments(
    classify: bool = Query(False),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Retorna todos os comentários salvos no banco, de todos os vídeos."""
    q = db.query(CommunityComment)
    if status:
        st = (status or "").strip().lower()
        if st in {"new", "reviewed", "replied"}:
            q = q.filter(CommunityComment.status == st)
        elif st == "pending":
            q = q.filter(CommunityComment.status != "replied")
    comments = q.order_by(CommunityComment.created_at.desc()).limit(300).all()

    if classify:
        ai = AIContentGenerator()
        updated = 0
        for c in comments:
            if updated >= 10:
                break
            if c.youtube_parent_id:
                continue
            if c.status == "replied":
                continue
            missing = (not (c.label or "").strip()) or (not (c.reply_draft or "").strip())
            if not missing:
                continue
            try:
                sys = "Você é um assistente pastoral. Classifique e redija resposta empática, breve e bíblica quando apropriado."
                prompt = f"""
Analise o comentário abaixo e devolva JSON com as chaves:
- label: uma de [elogio, duvida, critica, pedido_oracao, testemunho, sugestao_tema, spam, toxico]
- sentiment: positive|neutral|negative
- urgency: low|medium|high
- draft_reply: texto breve (PT-BR), respeitoso, sem promessas irreais, citando referência bíblica opcional.

Comentário: \"\"\"{(c.text or '').strip()}\"\"\"
"""
                raw = ai._generate_text(prompt, system_prompt=sys, json_mode=True)
                data = json.loads(raw or "{}")
                c.label = (data.get("label") or "").strip() or c.label
                c.sentiment = (data.get("sentiment") or "").strip() or c.sentiment
                c.urgency = (data.get("urgency") or "").strip() or c.urgency
                if (data.get("draft_reply") or "").strip():
                    c.reply_draft = (data.get("draft_reply") or "").strip()
                if c.status in {None, "", "new"}:
                    c.status = "reviewed"
                updated += 1
            except Exception:
                pass
        if updated:
            db.commit()
    items = []
    for c in comments:
        items.append({
            "youtube_comment_id": c.youtube_comment_id,
            "youtube_parent_id": c.youtube_parent_id,
            "youtube_video_id": c.youtube_video_id,
            "author": c.author,
            "text": c.text,
            "like_count": c.like_count or 0,
            "published_at": c.published_at.isoformat() if c.published_at else None,
            "status": c.status,
            "label": c.label,
            "sentiment": c.sentiment,
            "urgency": c.urgency,
            "reply_draft": c.reply_draft,
            "reply_text": c.reply_text,
            "reply_sent_at": c.reply_sent_at.isoformat() if c.reply_sent_at else None,
        })
    return {"youtube_video_id": "all", "count": len(items), "items": items}


# ─── Community Posts: AI-generated Posters & Polls ──────
class CommunityPostCreate(BaseModel):
    post_type: str = "poster"  # poster | poll
    topic: Optional[str] = None

class CommunityPollVote(BaseModel):
    option_index: int

@router.post("/community/posts/generate")
def generate_community_post(req: CommunityPostCreate, db: Session = Depends(get_db)):
    """Gera um poster ou enquete para a comunidade usando IA."""
    ai = AIContentGenerator()
    post_type = (req.post_type or "poster").lower()

    if post_type == "poster":
        prompt = f"""Crie um post engajante para a comunidade de um canal do YouTube.
Tema: {req.topic or 'motivação e crescimento pessoal'}

Retorne APENAS um JSON válido:
{{
    "title": "Título chamativo do post (máx 80 caracteres)",
    "content": "Texto completo do post (2-4 parágrafos, engajante, com emojis, CTA no final pedindo opinião dos inscritos)",
    "image_prompt": "Prompt em inglês para gerar imagem (estilo poster motivacional, cores vibrantes)"
}}"""
        sys_prompt = "Você é um especialista em community management do YouTube. Crie conteúdo que incentive a participação."
    else:
        prompt = f"""Crie uma enquete engajante para a comunidade de um canal do YouTube.
Tema: {req.topic or 'preferências do público sobre conteúdo'}

A enquete deve gerar debate e participação ativa. Crie opções que cubram diferentes perspectivas.

Retorne APENAS um JSON válido:
{{
    "title": "Pergunta principal da enquete (direta, provocativa, máx 120 caracteres)",
    "content": "Texto contextualizando a enquete (1-2 parágrafos, explica por que a opinião deles importa)",
    "options": ["Opção 1", "Opção 2", "Opção 3", "Opção 4"]
}}"""
        sys_prompt = "Você é um especialista em engajamento de comunidade no YouTube. Crie enquetes que gerem discussão."

    try:
        raw = ai._generate_text(prompt, system_prompt=sys_prompt, json_mode=True)
        data = json.loads((raw or "{}").replace("```json", "").replace("```", "").strip())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar conteúdo: {e}")

    post = CommunityPost(
        post_type=post_type,
        title=data.get("title", "Post da Comunidade"),
        content=data.get("content", ""),
        image_prompt=data.get("image_prompt"),
        status="draft",
    )
    if post_type == "poll":
        options = data.get("options", [])
        post.poll_options_json = json.dumps([{"text": opt, "votes": 0} for opt in options])
        post.poll_votes_json = json.dumps({})
    db.add(post)
    db.commit()
    db.refresh(post)

    result = {
        "id": post.id,
        "post_type": post.post_type,
        "title": post.title,
        "content": post.content,
        "image_prompt": post.image_prompt,
        "status": post.status,
        "created_at": post.created_at.isoformat() if post.created_at else None,
    }
    if post_type == "poll":
        result["poll_options"] = json.loads(post.poll_options_json or "[]")
    return result


@router.get("/community/posts")
def list_community_posts(db: Session = Depends(get_db)):
    """Lista todos os posts da comunidade."""
    posts = db.query(CommunityPost).order_by(CommunityPost.created_at.desc()).limit(50).all()
    items = []
    for p in posts:
        item = {
            "id": p.id,
            "post_type": p.post_type,
            "title": p.title,
            "content": p.content,
            "image_prompt": p.image_prompt,
            "image_url": p.image_url,
            "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        if p.post_type == "poll":
            item["poll_options"] = json.loads(p.poll_options_json or "[]")
        items.append(item)
    return {"count": len(items), "items": items}


@router.post("/community/posts/{post_id}/publish")
def publish_community_post(post_id: int, db: Session = Depends(get_db)):
    """Marca um post como publicado."""
    post = db.query(CommunityPost).filter(CommunityPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post não encontrado")
    post.status = "published"
    db.commit()
    return {"status": "published", "id": post.id}


@router.post("/community/posts/{post_id}/vote")
def vote_community_poll(post_id: int, req: CommunityPollVote, db: Session = Depends(get_db)):
    """Registra um voto em uma enquete."""
    post = db.query(CommunityPost).filter(CommunityPost.id == post_id).first()
    if not post or post.post_type != "poll":
        raise HTTPException(status_code=404, detail="Enquete não encontrada")

    options = json.loads(post.poll_options_json or "[]")
    if req.option_index < 0 or req.option_index >= len(options):
        raise HTTPException(status_code=400, detail="Opção inválida")

    options[req.option_index]["votes"] = options[req.option_index].get("votes", 0) + 1
    post.poll_options_json = json.dumps(options)
    db.commit()
    return {"poll_options": options}


@router.delete("/community/posts/{post_id}")
def delete_community_post(post_id: int, db: Session = Depends(get_db)):
    """Remove um post da comunidade."""
    post = db.query(CommunityPost).filter(CommunityPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post não encontrado")
    db.delete(post)
    db.commit()
    return {"status": "deleted"}
