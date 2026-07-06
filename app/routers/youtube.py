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
from typing import Any, Dict, List, Optional
import requests
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
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
from app.services.task_manager import create_task, update_task, get_task, is_task_cancel_requested, request_cancel_task, reset_task_for_retry
from app.services.youtube_auto_responder import auto_thank_comments
from app.database import get_db, SessionLocal
from app.services.video_factory import VideoFactory
from app.models import ScheduledVideo, ChannelReport, Settings, ContentPlan, Video, Job, Asset, Scene, CommunityComment, CommunityPost, StoryDraft, SystemNotification, ChannelInsight, VideoTask, User
from app.modules.ai_factory.models import AIImage
from app.redis_client import conn, queue as rq_queue
from app.routers.auth import get_current_admin_user, SECRET_KEY as _AUTH_SECRET_KEY, ALGORITHM as _AUTH_ALGORITHM

FACTORY_LOCK_KEY = "codexia:video_factory:single_worker_lock"
_CANCEL_ALL_KEY = "codexia:video_cancel_all"
_CANCEL_ALL_TTL_SECONDS = 90
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

def _rq_video_timeout_seconds() -> int:
    raw = (os.getenv("RQ_VIDEO_TIMEOUT") or os.getenv("RQ_DEFAULT_TIMEOUT") or "").strip()
    try:
        v = int(raw) if raw else 14400
    except Exception:
        v = 14400
    return max(600, v)

def _rq_workers_online() -> bool:
    """Retorna True quando há pelo menos um worker RQ ouvindo a fila."""
    if not conn or not RQ_AVAILABLE or Worker is None:
        return False
    try:
        workers = []
        try:
            workers = list(Worker.all(conn))
        except Exception:
            workers = []
        if not workers:
            try:
                return Worker.count(conn) > 0
            except Exception:
                return False
        now = datetime.utcnow()
        for w in workers:
            try:
                hb = getattr(w, "last_heartbeat", None)
                if hb:
                    try:
                        age = (now - hb).total_seconds()
                    except Exception:
                        age = None
                    if age is not None and age <= 120:
                        return True
            except Exception:
                continue
        return False
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

def _load_story_video_task_rows(db: Session, limit: int = 50) -> List[VideoTask]:
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.status.in_(["pending", "processing"]))
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
    filtered.sort(key=lambda r: (0 if str(r.status or "").lower() == "processing" else 1, r.created_at or datetime.utcnow(), str(r.id)))
    return filtered


def _story_video_stale_minutes() -> int:
    try:
        raw = int((os.getenv("VIDEO_TASK_STALE_MINUTES") or "").strip() or "180")
    except Exception:
        raw = 180
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
    is_current = str(row.status or "").lower() == "processing"
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
        "queue_label": "Em execução" if is_current else "Na fila",
        "can_open": True,
        "can_cancel": True,
        "cancel_kind": "task",
    }

def _active_video_task_blocker_item(db: Session, excluded_task_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    excluded = {str(v) for v in (excluded_task_ids or set()) if str(v).strip()}
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.status.in_(["pending", "processing"]))
        .order_by(VideoTask.updated_at.desc().nullslast(), VideoTask.created_at.desc().nullslast())
        .limit(50)
        .all()
    )
    for row in rows:
        if str(row.id) in excluded:
            continue
        if str(row.status or "").lower() != "processing":
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
        "queue_label": "Ocupando o servidor",
        "can_open": False,
        "can_cancel": True,
        "cancel_kind": "production_video",
        "production_video_id": int(video.id),
    }

def _load_factory_blocker_item(db: Session, excluded_task_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    item = _active_video_task_blocker_item(db, excluded_task_ids=excluded_task_ids)
    if item:
        return item
    return _active_production_video_blocker_item(db)

def _dispatch_video_generation_task(payload: Dict[str, Any], task_id: str):
    use_rq_raw = (os.getenv("USE_RQ_FOR_VIDEO_GENERATION") or "").strip()
    if use_rq_raw:
        use_rq = use_rq_raw.lower() in {"1", "true", "yes"}
    else:
        use_rq = conn is not None and _rq_workers_online()
    allow_inline_raw = os.getenv("ALLOW_INLINE_VIDEO_GENERATION")
    force_local_reason = None

    if use_rq:
        if conn is None or not _rq_workers_online():
            use_rq = False
            force_local_reason = "Worker/RQ indisponível. Iniciando geração local automaticamente..."
        try:
            if use_rq:
                rq_queue.enqueue(process_video_generation_payload, payload, task_id, job_timeout=_rq_video_timeout_seconds())
                update_task(task_id, status="processing", progress=1, message="Enfileirado para processamento em segundo plano...", result={"payload": payload, "executor": "rq", "kind": "youtube_story_video"})
                def _watchdog_fallback(tid: str, pay: Dict[str, Any]):
                    try:
                        raw = (os.getenv("VIDEO_TASK_QUEUE_STALE_SECONDS") or "").strip()
                        wait_s = int(raw) if raw else 90
                    except Exception:
                        wait_s = 90
                    wait_s = max(20, min(15 * 60, wait_s))
                    try:
                        time.sleep(wait_s)
                    except Exception:
                        return
                    try:
                        if is_task_cancel_requested(tid):
                            return
                    except Exception:
                        pass
                    t = get_task(tid) or {}
                    status = str((t.get("status") or "")).lower()
                    msg = str((t.get("message") or ""))
                    try:
                        p = int(t.get("progress") or 0)
                    except Exception:
                        p = 0
                    if status == "processing" and p <= 1 and ("enfileirad" in msg.lower() or "enfileirando" in msg.lower()):
                        try:
                            update_task(tid, status="processing", progress=2, message="Fila sem worker ativo. Iniciando execução local...")
                        except Exception:
                            pass
                        try:
                            th = threading.Thread(target=process_video_generation_payload, args=(pay, tid), daemon=True)
                            th.start()
                        except Exception:
                            pass
                threading.Thread(target=_watchdog_fallback, args=(task_id, payload), daemon=True).start()
                return
        except Exception as e:
            try:
                update_task(task_id, status="pending", progress=0, message=f"Falha ao enfileirar no worker. Aguardando retry automático... ({str(e)[:200]})")
            except Exception:
                pass
            return

    if allow_inline_raw is None or not str(allow_inline_raw).strip():
        allow_inline = True
    else:
        allow_inline = str(allow_inline_raw).strip().lower() in {"1", "true", "yes", "on"}
    if not allow_inline and not force_local_reason:
        try:
            update_task(task_id, status="pending", progress=0, message="Aguardando worker em segundo plano (RQ) para iniciar a geração...")
        except Exception:
            pass
        return

    executor = (os.getenv("VIDEO_GENERATION_EXECUTOR") or "thread").strip().lower()
    if executor not in {"auto", "thread", "process"}:
        executor = "thread"

    use_process = (executor == "process") and (conn is not None)
    if use_process:
        try:
            method = "spawn" if sys.platform == "win32" else "fork"
            ctx = multiprocessing.get_context(method)
            p = ctx.Process(target=process_video_generation_payload, args=(payload, task_id), daemon=True)
            p.start()
            update_task(task_id, status="processing", progress=1, message="Iniciando geração em processo separado...", result={"payload": payload, "executor": "process", "pid": p.pid, "kind": "youtube_story_video"})
            def _watch(proc: multiprocessing.Process, tid: str):
                try:
                    proc.join()
                    if proc.exitcode and proc.exitcode != 0:
                        t = get_task(tid) or {}
                        status = str((t.get("status") or "")).lower()
                        if status not in {"completed", "failed", "cancelled"}:
                            update_task(tid, status="failed", progress=0, message="Falha ao iniciar/rodar o processo de geração. Verifique os logs do container.")
                except Exception:
                    pass
            threading.Thread(target=_watch, args=(p, task_id), daemon=True).start()
            return
        except Exception:
            pass

    msg = force_local_reason or "Iniciando geração local..."
    if executor == "process" and conn is None:
        msg = "Redis indisponível para acompanhar progresso em processo separado. Iniciando geração local..."
    update_task(task_id, status="processing", progress=1, message=msg, result={"payload": payload, "executor": "thread", "kind": "youtube_story_video"})
    t = threading.Thread(target=process_video_generation_payload, args=(payload, task_id), daemon=True)
    t.start()

def _kick_story_video_task_queue() -> Optional[str]:
    db = SessionLocal()
    try:
        rows = _load_story_video_task_rows(db, limit=100)
        if not rows:
            return None
        cleanup_info = _cleanup_story_video_task_queue(db, rows=rows)
        if cleanup_info.get("changed"):
            rows = _load_story_video_task_rows(db, limit=100)
            if not rows:
                return None
        processing = next((r for r in rows if str(r.status or "").lower() == "processing"), None)
        if processing:
            return processing.id
        if _is_video_factory_busy():
            return None
        pending = next((r for r in rows if str(r.status or "").lower() == "pending"), None)
        if not pending:
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
    threading.Thread(target=_kick_story_video_task_queue, daemon=True).start()

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
            "youtube_id": v.youtube_video_id
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

        is_error = False
        youtube_id = None
        if isinstance(upload_result, dict):
            if upload_result.get("error"):
                is_error = True
            elif _is_mock_upload(upload_result):
                is_error = True
            else:
                youtube_id = upload_result.get("id") or str(upload_result)
        else:
            youtube_id = str(upload_result) if upload_result else None
            if not youtube_id:
                is_error = True

        if is_error or not youtube_id:
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
    background_tasks.add_task(process_jobs_background)
    return {"status": "queued", "step": next_step, "message": "Produção retomada com sucesso."}

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
    thumbnail_path: Optional[str] = None
    override_title: Optional[str] = None
    override_description: Optional[str] = None
    override_tags: Optional[List[str]] = None
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None
    image_mode: Optional[str] = None  # auto | single | multiple

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
            p_list = ai_service.generate_story_image_prompts(chunk, n=1, kind=kind) or []
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

        from app.services.video_generator import VideoGenerator
        video_service = VideoGenerator(ai_service=ai_service)

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

            def _video_progress(p, m, short_idx=idx, total=count):
                base = 10 + int((short_idx / max(1, total)) * 80)
                span = int((1 / max(1, total)) * 80)
                mapped = min(95, base + int((p or 0) / 100 * max(1, span)))
                _progress(mapped, f"Short {short_idx+1}/{total}: {m}")

            result = video_service.create_video_from_plan(
                plan,
                aspect_ratio="9:16",
                progress_callback=_video_progress,
                voice_style=voice_style,
                voice_gender=voice_gender,
            )
            video_url = result.get("video_url") if isinstance(result, dict) else None
            shorts.append({
                "title": plan.get("title") or f"Short {idx+1}",
                "description": plan.get("description") or "",
                "video_url": video_url,
                "kind": kind,
                "video_type": "short",
            })

        _progress(100, "Shorts prontos.")
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
        text = ai_service.generate_story_or_devotional_text(
            instruction=request.instruction,
            kind=kind,
            duration_min_minutes=request.duration_min,
            duration_max_minutes=request.duration_max,
        )
        return {"text": text, "kind": kind, "duration_min": request.duration_min, "duration_max": request.duration_max}
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
                "source": "derived_from_scheduled",
                "source_scheduled_video_id": scheduled.id,
                "parent_video_id": scheduled.id,
                "kind": kind,
                "video_type": "short",
                "title": title,
                "description": description,
                "video_url": video_url,
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
    """Envia um vídeo já gerado para a fila 'Aguardando Publicação'."""
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

    payload = {
        "source": "generated_story",
        "kind": kind,
        "video_type": video_type,
        "title": title,
        "description": description,
        "video_url": video_url,
    }
    if request.voice_style:
        payload["voice_style"] = request.voice_style
    if request.voice_gender:
        payload["voice_gender"] = request.voice_gender

    video = ScheduledVideo(
        theme="História/Devocional",
        title=title,
        description=description,
        scheduled_for=scheduled_for,
        video_type=video_type,
        script_data=json.dumps(payload),
        status="completed",
        auto_post=bool(request.auto_post),
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

    return {"id": video.id, "status": video.status, "video_url": video.video_url}

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
        "db_client_id_prefix": (settings.youtube_client_id[:5] + "...") if (settings and settings.youtube_client_id) else None,
        "db_has_client_secret": bool(settings and settings.youtube_client_secret),
        "db_has_refresh_token": bool(settings and settings.youtube_refresh_token),
        "db_refresh_token_prefix": (settings.youtube_refresh_token[:5] + "...") if (settings and settings.youtube_refresh_token) else None,
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
def get_auth_url(db: Session = Depends(get_db)):
    """Retorna sempre JSON. Verifica credenciais antes de instanciar YouTubeService."""
    try:
        # Verificar se há credenciais no banco (evita exceção genérica ao instanciar o serviço)
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
        auth_url = service.get_auth_url()
        if not auth_url:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a URL de autorização. Verifique se Client ID e Client Secret em Configurações estão corretos (Google Cloud Console)."
            )
        return {"auth_url": auth_url}
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

@router.post("/auth/exchange")
def exchange_code(data: Dict[str, str]):
    code = data.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Código não fornecido")
    
    # Sanitizar código: espaços e quebras de linha ao copiar do Google quebram a troca
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
                   "Verifique:\n"
                   "1. O código foi copiado corretamente (sem espaços, quebras de linha)\n"
                   "2. O código não expirou (códigos de autorização expiram em ~10 minutos)\n"
                   "3. O Client ID e Client Secret estão configurados corretamente\n"
                   "4. A API 'YouTube Data API v3' está ativada no Google Cloud Console\n"
                   "5. O tipo de aplicativo é 'Desktop' ou 'Web' com redirect URI 'urn:ietf:wg:oauth:2.0:oob'"
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
    
    # Simple migration check for SQLite
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
    
    for item in plan:
        # Extrair dados do item
        # Se for music_mode, o item já deve vir com music_file_path
        
        video = ScheduledVideo(
            theme=item.get("theme_of_day", "Geral"),
            title=item.get("videos", [{}])[0].get("title", "Vídeo Agendado") if isinstance(item.get("videos"), list) else item.get("title", "Vídeo"),
            description=item.get("videos", [{}])[0].get("concept", "") if isinstance(item.get("videos"), list) else item.get("concept", ""),
            scheduled_for=datetime.strptime(f"{item.get('date')} {item.get('videos', [{}])[0].get('time', '12:00')}", "%Y-%m-%d %H:%M") if item.get("date") else datetime.now(),
            video_type=item.get("videos", [{}])[0].get("type", "video") if isinstance(item.get("videos"), list) else item.get("type", "video"),
            script_data=json.dumps(item.get("videos", [{}])[0]) if isinstance(item.get("videos"), list) else json.dumps(item),
            status="queued", # Start as queued
            auto_post=item.get("videos", [{}])[0].get("auto_post", True) if isinstance(item.get("videos"), list) else item.get("auto_post", True),
            voice_style=item.get("videos", [{}])[0].get("voice_style", "human") if isinstance(item.get("videos"), list) else item.get("voice_style", "human"),
            voice_gender=item.get("videos", [{}])[0].get("voice_gender", "female") if isinstance(item.get("videos"), list) else item.get("voice_gender", "female"),
            music_file_path=item.get("videos", [{}])[0].get("music_file_path") if isinstance(item.get("videos"), list) else item.get("music_file_path")
        )
        db.add(video)
        db.flush() # get ID
        saved_videos.append(video)
    
    db.commit()
    
    # Kickoff imediato do primeiro item para não depender exclusivamente do scheduler
    # (evita sensação de "não está gerando").
    if saved_videos:
        try:
            processing = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").first()
            if not processing:
                from app.services.video_processing import process_scheduled_video
                background_tasks.add_task(process_scheduled_video, saved_videos[0].id)
        except Exception as e:
            print(f"Erro ao iniciar geração imediata: {e}")
    
    return {"message": "Schedule saved", "count": len(saved_videos)}

@router.post("/schedule/{video_id}/generate")
def generate_scheduled_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Limpar cache de script se existir, para forçar regeneração da IA (pois o usuário pediu explicitamente)
    if video.script_data:
        try:
            data = json.loads(video.script_data)
            changed = False
            # Remove chaves geradas pela IA para garantir novo conteúdo
            keys_to_remove = ["scenes", "audio_path", "background_music", "music_credit"]
            for k in keys_to_remove:
                if k in data:
                    del data[k]
                    changed = True
            
            if changed:
                video.script_data = json.dumps(data)
        except Exception as e:
            print(f"Erro ao limpar cache do script: {e}")

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
        markers = ("[ERRO]", "[SISTEMA]", "[UPLOAD_ERRO]")
        cleaned_lines = [ln for ln in video.description.splitlines() if not any(m in ln for m in markers)]
        video.description = "\n".join(cleaned_lines).strip()

    video.status = "queued"
    video.progress = 0 # Reset progress
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
            background_tasks.add_task(process_scheduled_video, video_id)
    except Exception as e:
        print(f"Erro ao iniciar regeneração imediata do vídeo {video_id}: {e}")

    return {"status": "queued"}

@router.post("/schedule/{video_id}/regenerate")
def regenerate_scheduled_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Mesma coisa que generate, mas semanticamente explícito"""
    return generate_scheduled_video(video_id, background_tasks, db)

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

    is_error = False
    video_id_value = None
    if isinstance(upload_result, dict):
        if upload_result.get("error"):
            is_error = True
        elif _is_mock_upload(upload_result):
            is_error = True
        else:
            video_id_value = upload_result.get("id") or str(upload_result)
    else:
        video_id_value = str(upload_result) if upload_result else None
        if not video_id_value:
            is_error = True

    if is_error or not video_id_value:
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
    return {"status": "published", "youtube_video_id": video_id_value, "message": "Vídeo publicado com sucesso!"}

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

    is_error = False
    video_id_value = None
    if isinstance(upload_result, dict):
        if upload_result.get("error"):
            is_error = True
        elif _is_mock_upload(upload_result):
            is_error = True
        else:
            video_id_value = upload_result.get("id") or str(upload_result)
    else:
        video_id_value = str(upload_result) if upload_result else None
        if not video_id_value:
            is_error = True

    if is_error or not video_id_value:
        err_msg = _publish_error_message(upload_result, action_label="republicar")
        video.description = _append_upload_error_note(video.description, err_msg)
        db.commit()
        raise HTTPException(status_code=502, detail=err_msg)

    video.uploaded_at = datetime.now()
    video.youtube_video_id = video_id_value
    video.status = "published"
    db.commit()
    return {"status": "published", "youtube_video_id": video_id_value, "message": "Vídeo republicado com sucesso!"}

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
def generate_video(request: VideoRequest, background_tasks: BackgroundTasks):
    """Gera um vídeo motivacional e opcionalmente faz upload"""
    
    if _cancel_all_active():
        raise HTTPException(status_code=409, detail="Encerramento geral em andamento no servidor. Aguarde ~1 minuto e tente novamente.")

    try:
        payload = request.model_dump()  # type: ignore[attr-defined]
    except Exception:
        payload = request.dict()

    task_id = create_task()
    base_result = {
        "payload": payload,
        "kind": "youtube_story_video",
        "title_hint": _story_video_task_title_from_payload(payload),
    }
    update_task(task_id, status="pending", progress=0, message="Aguardando vez na fila de produção...", result=base_result)

    db = SessionLocal()
    try:
        queue_rows = _load_story_video_task_rows(db, limit=100)
        queue_ids = [str(r.id) for r in queue_rows]
        queue_position = queue_ids.index(str(task_id)) + 1 if str(task_id) in queue_ids else None
        already_processing = any(str(r.status or "").lower() == "processing" for r in queue_rows if str(r.id) != str(task_id))
    finally:
        db.close()

    _kick_story_video_task_queue_async()
    return {
        "message": "Vídeo enviado para a fila de produção.",
        "task_id": task_id,
        "queued": bool(already_processing or (queue_position and queue_position > 1)),
        "queue_position": queue_position or 1,
    }

@router.get("/task/{task_id}")
def get_task_status(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    try:
        if status in {"processing"}:
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
    return task

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
        rows = _load_story_video_task_rows(db, limit=limit)
        factory_busy = bool(_is_video_factory_busy())
        items: List[Dict[str, Any]] = []
        if factory_busy and not any(str(r.status or "").lower() == "processing" for r in rows):
            blocker = _load_factory_blocker_item(db, excluded_task_ids={str(r.id) for r in rows})
            if blocker:
                items.append(blocker)
        for row in rows:
            items.append(_story_video_task_item_from_row(row, len(items) + 1))
        items = items[: max(1, min(200, int(limit or 20)))]
        for idx, item in enumerate(items, start=1):
            item["position"] = idx
        payload = {
            "count": len(items),
            "processing_count": len([i for i in items if bool(i.get("is_current"))]),
            "items": items,
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
    if status != "completed":
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
    if status != "completed":
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

@router.post("/tasks/cancel_all")
def cancel_all_tasks(_admin=Depends(get_current_admin_user)):
    if conn:
        try:
            conn.set(_CANCEL_ALL_KEY, "1", ex=_CANCEL_ALL_TTL_SECONDS)
        except Exception:
            pass
        try:
            conn.delete(FACTORY_LOCK_KEY)
        except Exception:
            pass

    try:
        if os.path.exists(_FACTORY_LOCK_PATH):
            os.remove(_FACTORY_LOCK_PATH)
    except Exception:
        pass

    db = SessionLocal()
    try:
        rows = db.query(VideoTask).filter(VideoTask.status.in_(["pending", "processing"])).all()
        for r in rows:
            r.status = "cancelled"
            r.message = "Cancelado pelo usuário (encerrar produção do servidor)."
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

    return {"status": "ok", "message": "Solicitação enviada. Aguarde ~1 minuto para o servidor encerrar as tarefas em andamento e liberar a produção."}

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
def retry_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
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
    if status == "processing" and progress_n >= 5:
        raise HTTPException(status_code=409, detail="Tarefa já está em processamento.")
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    payload = (result or {}).get("payload") if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Não há payload salvo para reiniciar esta tarefa.")
    try:
        req = VideoRequest(**payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido para reiniciar a tarefa.")
    reset_task_for_retry(task_id, progress=1, message="Reiniciando geração local...")
    t = threading.Thread(target=process_video_generation, args=(req, task_id), daemon=True)
    t.start()
    return {"message": "Reiniciado", "task_id": task_id}

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

    # Memory Check
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
            mem_total = int([l.split()[1] for l in lines if l.startswith('MemTotal')][0]) / 1024
            mem_avail = int([l.split()[1] for l in lines if l.startswith('MemAvailable')][0]) / 1024
            checks.append({"name": "Memória (MB)", "ok": mem_avail > 300, "value": f"Livre: {mem_avail:.0f} MB / Total: {mem_total:.0f} MB"})
    except Exception:
        pass

    report: Dict[str, Any] = {
        "task_id": task_id,
        "checks": checks,
        "task": None,
        "recommendations": [],
        "ai": None,
    }

    if task_id:
        t = get_task(task_id)
        report["task"] = t
        if not t:
            report["recommendations"].append("Task não encontrada: confirme se o deploy é o mesmo servidor e se o task_id é válido.")
        else:
            status = str((t.get("status") or "")).lower()
            msg = str((t.get("message") or ""))
            if status in {"pending", "processing"} and ("enfileirando" in msg.lower() or "separado" in msg.lower() or (t.get("progress") in (0, 1))):
                report["recommendations"].append("O processo parece travado no início. A geração via processo/thread pode ter falhado silenciosamente. Use o botão Reiniciar para forçar a execução local via thread na API.")
            if use_rq and (not workers):
                report["recommendations"].append("USE_RQ_FOR_VIDEO_GENERATION está ativo, mas não há workers RQ. Desative USE_RQ_FOR_VIDEO_GENERATION ou suba um worker.")
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

    def _raise_if_cancelled():
        t = get_task(task_id) or {}
        if str((t.get("status") or "")).lower() == "cancelled" or _cancel_all_active():
            raise _TaskCancelled()

    redis_lock = None
    file_lock = None
    try:
        _raise_if_cancelled()
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("MKL_NUM_THREADS", "1")
        os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
        if conn:
            try:
                redis_lock = conn.lock(FACTORY_LOCK_KEY, timeout=4 * 60 * 60, blocking_timeout=1)
                if not redis_lock.acquire(blocking=False):
                    update_task(task_id, status="pending", progress=0, message="Servidor ocupado. Aguardando vez na fila de produção...")
                    return
            except Exception:
                redis_lock = None

        if not conn or not redis_lock:
            try:
                file_lock = FileLock(_FACTORY_LOCK_PATH, timeout=0)
                file_lock.acquire()
            except Timeout:
                update_task(task_id, status="pending", progress=0, message="Servidor ocupado. Aguardando vez na fila de produção...")
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
        update_task(task_id, status="processing", progress=5, message=f"Iniciando geração sobre: {topic_display}")
        print(f"Iniciando geração de vídeo ({request.mode}): {topic_display}")
        
        ai_service = AIContentGenerator()
        video_service = VideoGenerator(ai_service=ai_service)
        yt_service = YouTubeService()
        
        # 1. Gerar Roteiro
        update_task(task_id, progress=10, message="Estruturando roteiro com IA...")
        _raise_if_cancelled()
        
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

        if request.mode == 'story' and request.story_content:
            minutes = 10
            try:
                minutes = requested_minutes
            except Exception:
                minutes = 10
            minutes = max(1, min(60, minutes))
            script = _build_story_plan_from_text(request.story_content, minutes, kind_norm)
        else:
            # Fallback to topic mode if no story content
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
                    spm = float(spm_raw) if spm_raw else 1.6
                except Exception:
                    spm = 1.6
                spm = max(0.8, min(3.0, spm))
                try:
                    max_raw = (os.getenv("YOUTUBE_SCENES_MAX") or "").strip()
                    max_scenes = int(max_raw) if max_raw else 60
                except Exception:
                    max_scenes = 60
                max_scenes = max(20, min(120, max_scenes))
                min_scenes = 4 if m <= 2 else 6
                return max(min_scenes, min(max_scenes, int(m * spm)))

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
        
        # 2. Gerar Vídeo (16:9)
        # Passamos uma função de callback para atualizar o progresso
        def progress_callback(progress, message):
            # Mapeia progresso do vídeo (0-100) para progresso da tarefa (20-90)
            task_progress = 20 + int(progress * 0.7)
            update_task(task_id, progress=task_progress, message=message)
            _raise_if_cancelled()
            
        video_result = video_service.create_video_from_plan(
            script,
            aspect_ratio="16:9",
            progress_callback=progress_callback,
            voice_style=voice_style,
            voice_gender=voice_gender,
        )
        video_path = video_result["video_url"]
        render_report = video_result.get("render_report") if isinstance(video_result, dict) else {}
        if not isinstance(render_report, dict):
            render_report = {}
        sync_validation = render_report.get("sync_validation")
        audio_generation = render_report.get("audio_generation")
        _raise_if_cancelled()
        
        # Path absoluto para upload (compatível com Docker e /data/media)
        from app.config import absolute_path_for_video
        abs_video_path = absolute_path_for_video(video_path)
        print(f"Vídeo gerado em: {abs_video_path}")
        
        # 3. Upload (se solicitado)
        if request.auto_upload:
            update_task(task_id, progress=90, message="Iniciando upload para o YouTube...")
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
            update_task(task_id, progress=100, status="completed", message="Vídeo gerado e publicado com sucesso!", result={
                "video_url": video_path,
                "title": script.get("title"),
                "description": description,
                "tags": script.get("tags"),
                "kind": "story" if request.mode == "story" else "topic",
                "audio_generation": audio_generation,
                "sync_validation": sync_validation,
            })
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
            update_task(task_id, progress=100, status="completed", message="Vídeo gerado com sucesso!", result={
                "video_url": video_path,
                "title": script.get("title"),
                "description": script.get("description"),
                "tags": script.get("tags"),
                "kind": "story" if request.mode == "story" else "topic",
                "audio_generation": audio_generation,
                "sync_validation": sync_validation,
            })
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
            
    except _TaskCancelled:
        try:
            t = get_task(task_id) or {}
            try:
                current_progress = int(t.get("progress") or 0)
            except Exception:
                current_progress = 0
            update_task(task_id, status="cancelled", progress=current_progress, message="Cancelado pelo usuário.")
        except Exception:
            pass
    except Exception as e:
        print(f"Erro na tarefa {task_id}: {e}")
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")
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
