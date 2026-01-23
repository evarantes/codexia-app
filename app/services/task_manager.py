import uuid
from typing import Dict, Any

# Armazenamento em memória para simplificar (em produção idealmente seria Redis ou DB)
# Estrutura: {task_id: {status: str, progress: int, message: str, result: Any}}
video_tasks: Dict[str, Dict[str, Any]] = {}

def create_task():
    task_id = str(uuid.uuid4())
    video_tasks[task_id] = {
        "status": "pending",
        "progress": 0,
        "message": "Aguardando início...",
        "result": None
    }
    return task_id

def update_task(task_id, status=None, progress=None, message=None, result=None):
    if task_id in video_tasks:
        if status:
            video_tasks[task_id]["status"] = status
        if progress is not None:
            video_tasks[task_id]["progress"] = progress
        if message:
            video_tasks[task_id]["message"] = message
        if result:
            video_tasks[task_id]["result"] = result

def get_task(task_id):
    return video_tasks.get(task_id)
