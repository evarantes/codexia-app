import uuid
import json
from typing import Dict, Any, Optional
try:
    from app.redis_client import conn as _redis_conn  # type: ignore
except Exception:
    _redis_conn = None

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

def create_task():
    task_id = str(uuid.uuid4())
    initial = {
        "status": "pending",
        "progress": 0,
        "message": "Aguardando início...",
        "result": None
    }
    video_tasks[task_id] = initial
    _redis_set(task_id, initial)
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

def get_task(task_id):
    return video_tasks.get(task_id) or _redis_get(task_id)
