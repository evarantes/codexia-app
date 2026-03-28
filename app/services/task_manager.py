import uuid
import json
from datetime import datetime
from typing import Dict, Any, Optional
try:
    from app.redis_client import conn as _redis_conn  # type: ignore
except Exception:
    _redis_conn = None
from app.database import SessionLocal
from app.models import PersistentTask

# Armazenamento em memória para simplificar (em produção idealmente seria Redis ou DB)
# Estrutura: {task_id: {status: str, progress: int, message: str, result: Any}}
video_tasks: Dict[str, Dict[str, Any]] = {}
_REDIS_PREFIX = "codexia:video_task:"

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

def _persist_task_record(task_id: str, data: Dict[str, Any], kind: str = "generic", payload: Any = None):
    db = SessionLocal()
    try:
        row = db.query(PersistentTask).filter(PersistentTask.task_id == task_id).first()
        if not row:
            row = PersistentTask(task_id=task_id, kind=kind or "generic")
            db.add(row)
        row.kind = kind or row.kind or "generic"
        row.status = str(data.get("status") or row.status or "pending")
        row.progress = int(data.get("progress") or 0)
        row.message = data.get("message")
        if payload is not None:
            try:
                row.payload_json = json.dumps(payload, ensure_ascii=False)
            except Exception:
                row.payload_json = None
        if data.get("result") is not None:
            try:
                row.result_json = json.dumps(data.get("result"), ensure_ascii=False)
            except Exception:
                row.result_json = json.dumps({"raw": str(data.get("result"))}, ensure_ascii=False)
        if row.status in {"processing", "pending"} and not row.started_at:
            row.started_at = datetime.utcnow()
        if row.status == "completed":
            row.completed_at = datetime.utcnow()
            row.error_text = None
        elif row.status == "failed":
            row.completed_at = datetime.utcnow()
            row.error_text = str(data.get("message") or "")
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _load_persistent_task(task_id: str) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        row = db.query(PersistentTask).filter(PersistentTask.task_id == task_id).first()
        if not row:
            return None
        result = None
        if row.result_json:
            try:
                result = json.loads(row.result_json)
            except Exception:
                result = {"raw": row.result_json}
        return {
            "status": row.status or "pending",
            "progress": int(row.progress or 0),
            "message": row.message or "Aguardando início...",
            "result": result,
            "kind": row.kind or "generic",
        }
    except Exception:
        return None
    finally:
        db.close()


def create_task(kind: str = "generic", payload: Any = None):
    task_id = str(uuid.uuid4())
    initial = {
        "status": "pending",
        "progress": 0,
        "message": "Aguardando início...",
        "result": None
    }
    video_tasks[task_id] = initial
    _redis_set(task_id, initial)
    _persist_task_record(task_id, initial, kind=kind, payload=payload)
    return task_id

def update_task(task_id, status=None, progress=None, message=None, result=None):
    current = video_tasks.get(task_id) or _redis_get(task_id) or {
        "status": "processing",
        "progress": 0,
        "message": "Iniciando...",
        "result": None
    }
    if status:
        current["status"] = status
    if progress is not None:
        current["progress"] = progress
    if message:
        current["message"] = message
    if result:
        current["result"] = result
    video_tasks[task_id] = current
    _redis_set(task_id, current)
    _persist_task_record(task_id, current)

def get_task(task_id):
    return video_tasks.get(task_id) or _redis_get(task_id) or _load_persistent_task(task_id)
