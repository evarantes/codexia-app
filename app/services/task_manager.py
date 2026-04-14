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
    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if not row:
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
