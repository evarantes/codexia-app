import uuid
import json
from typing import Dict, Any, Optional
from datetime import datetime
try:
    from app.redis_client import conn as _redis_conn  # type: ignore
except Exception:
    _redis_conn = None
from app.database import SessionLocal
from app.models import VideoTask

# Armazenamento em memória para simplificar (em produção idealmente seria Redis ou DB)
# Estrutura: {task_id: {status: str, progress: int, message: str, result: Any}}
video_tasks: Dict[str, Dict[str, Any]] = {}
_REDIS_PREFIX = "codexia:video_task:"
_CONTROL_PREFIX = "codexia:video_task_control:"
_task_controls: Dict[str, Dict[str, Any]] = {}

def _control_get(task_id: str) -> Dict[str, Any]:
    base = _task_controls.get(task_id) or {}
    if not _redis_conn:
        return dict(base)
    try:
        raw = _redis_conn.get(_CONTROL_PREFIX + task_id)
        if not raw:
            return dict(base)
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            merged = dict(base)
            merged.update(data)
            _task_controls[task_id] = merged
            return merged
    except Exception:
        pass
    return dict(base)

def _control_set(task_id: str, data: Dict[str, Any]):
    cur = _control_get(task_id)
    cur.update({k: v for k, v in (data or {}).items()})
    _task_controls[task_id] = cur
    if not _redis_conn:
        return
    try:
        _redis_conn.set(_CONTROL_PREFIX + task_id, json.dumps(cur), ex=60 * 60 * 24)
    except Exception:
        pass

def is_task_deleted(task_id: str) -> bool:
    c = _control_get(task_id)
    return bool(c.get("deleted") is True)

def is_task_cancel_requested(task_id: str) -> bool:
    c = _control_get(task_id)
    return bool(c.get("cancel") is True)

def request_cancel_task(task_id: str, message: str = "Cancelado pelo usuário.") -> Optional[Dict[str, Any]]:
    _control_set(task_id, {"cancel": True})
    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if not row:
            return None
        row.status = "cancelled"
        row.message = message
        db.commit()
        current = _db_to_dict(row)
        video_tasks[task_id] = current
        _redis_set(task_id, current)
        return current
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()

def mark_task_deleted(task_id: str):
    _control_set(task_id, {"deleted": True})
    try:
        if task_id in video_tasks:
            video_tasks.pop(task_id, None)
    except Exception:
        pass
    if not _redis_conn:
        return
    try:
        _redis_conn.delete(_REDIS_PREFIX + task_id)
    except Exception:
        pass

def _redis_get(task_id: str) -> Optional[Dict[str, Any]]:
    if not _redis_conn:
        return None
    try:
        raw = _redis_conn.get(_REDIS_PREFIX + task_id)
        if not raw:
            return None
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None

def _redis_set(task_id: str, data: Dict[str, Any]):
    if not _redis_conn:
        return
    try:
        _redis_conn.set(_REDIS_PREFIX + task_id, json.dumps(data), ex=60 * 60)
    except Exception:
        pass

def _db_to_dict(row: VideoTask) -> Dict[str, Any]:
    result = None
    if row.result_json:
        try:
            result = json.loads(row.result_json)
        except Exception:
            result = row.result_json
    return {
        "task_id": row.id,
        "status": row.status,
        "progress": int(row.progress or 0),
        "message": row.message,
        "result": result,
        "created_at": (row.created_at.isoformat() if getattr(row, "created_at", None) else None),
        "updated_at": (row.updated_at.isoformat() if getattr(row, "updated_at", None) else None),
    }

def create_task(user_id: Optional[int] = None):
    task_id = str(uuid.uuid4())
    initial = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "message": "Aguardando início...",
        "result": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    db = SessionLocal()
    try:
        row = VideoTask(
            id=task_id,
            user_id=user_id,
            status="pending",
            progress=0,
            message="Aguardando início...",
            result_json=None,
        )
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    video_tasks[task_id] = initial
    _redis_set(task_id, initial)
    return task_id

def update_task(task_id, status=None, progress=None, message=None, result=None):
    if is_task_deleted(task_id):
        return
    if is_task_cancel_requested(task_id):
        try:
            if status and str(status).lower() not in {"cancelled"}:
                return
        except Exception:
            return
    db = SessionLocal()
    try:
        # #region debug-point D:task-progress-persist
        def _dbg_task_event(hypothesis_id, msg, data=None):
            try:
                import json as _json
                import urllib.request as _urlreq
                _p = ".dbg/render-stuck-86.env"
                _u, _s = "http://127.0.0.1:7777/event", "render-stuck-86"
                try:
                    with open(_p, "r", encoding="utf-8") as _f:
                        _c = _f.read()
                    for _line in _c.splitlines():
                        if _line.startswith("DEBUG_SERVER_URL="):
                            _u = _line.split("=", 1)[1].strip() or _u
                        elif _line.startswith("DEBUG_SESSION_ID="):
                            _s = _line.split("=", 1)[1].strip() or _s
                except Exception:
                    pass
                _payload = {
                    "sessionId": _s,
                    "runId": "pre-fix",
                    "hypothesisId": hypothesis_id,
                    "location": "app/services/task_manager.py:update_task",
                    "msg": f"[DEBUG] {msg}",
                    "data": data or {},
                }
                _req = _urlreq.Request(_u, data=_json.dumps(_payload).encode("utf-8"), headers={"Content-Type": "application/json"})
                _urlreq.urlopen(_req, timeout=2).read()
            except Exception:
                pass
        # #endregion
        row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if not row:
            if is_task_deleted(task_id) or is_task_cancel_requested(task_id):
                return
            row = VideoTask(
                id=task_id,
                status="processing",
                progress=0,
                message="Iniciando...",
                result_json=None,
            )
            db.add(row)
            db.commit()
            db.refresh(row)

        if str((row.status or "")).lower() in {"cancelled"} and (not status or str(status).lower() != "cancelled"):
            return
        if status:
            row.status = status
        if progress is not None:
            try:
                row.progress = int(progress)
            except Exception:
                row.progress = 0
        if message:
            row.message = message
        if result is not None:
            try:
                row.result_json = json.dumps(result, ensure_ascii=False)
            except Exception:
                row.result_json = json.dumps({"raw": str(result)}, ensure_ascii=False)
        try:
            _msg_lower = str(message or "").lower()
            _status_lower = str(status or row.status or "").lower()
            _progress_n = int(progress if progress is not None else (row.progress or 0))
            if _status_lower == "processing" and (_progress_n >= 85 or "render" in _msg_lower):
                _dbg_task_event("D", "task progress persisted", {
                    "task_id": str(task_id),
                    "status": _status_lower,
                    "progress": _progress_n,
                    "message": str(message or row.message or "")[:240],
                })
        except Exception:
            pass
        db.commit()
        current = _db_to_dict(row)
        video_tasks[task_id] = current
        _redis_set(task_id, current)
        return
    except Exception:
        db.rollback()
    finally:
        db.close()

    current = video_tasks.get(task_id) or _redis_get(task_id) or {
        "task_id": task_id,
        "status": "processing",
        "progress": 0,
        "message": "Iniciando...",
        "result": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    if status:
        current["status"] = status
    if progress is not None:
        current["progress"] = progress
    if message:
        current["message"] = message
    if result is not None:
        current["result"] = result
    video_tasks[task_id] = current
    _redis_set(task_id, current)

def get_task(task_id):
    if is_task_deleted(task_id):
        return None
    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if row:
            current = _db_to_dict(row)
            video_tasks[task_id] = current
            return current
    except Exception:
        pass
    finally:
        db.close()
    return video_tasks.get(task_id) or _redis_get(task_id)
