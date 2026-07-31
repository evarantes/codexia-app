import uuid
import json
import time
import threading
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
try:
    from app.redis_client import conn as _redis_conn  # type: ignore
except Exception:
    _redis_conn = None
from app.database import SessionLocal
from app.models import VideoTask
from sqlalchemy import text, inspect

# Armazenamento em memória para simplificar (em produção idealmente seria Redis ou DB)
# Estrutura: {task_id: {status: str, progress: int, message: str, result: Any}}
video_tasks: Dict[str, Dict[str, Any]] = {}
_REDIS_PREFIX = "codexia:video_task:"
_CONTROL_PREFIX = "codexia:video_task_control:"
_LOCK_PREFIX = "codexia:video_task_lock:"
_task_controls: Dict[str, Dict[str, Any]] = {}
_task_schema_ready = False
_task_schema_lock = threading.Lock()
_TASK_DEDUPE_TABLE = "video_task_dedupe"
_TASK_LEASE_TABLE = "video_task_leases"
_TASK_LOCK_TABLE = "video_task_locks"
_TASK_SCHEMA_REQUIRED_REVISION = "b8f4a7c9d321"
_TASK_SCHEMA_COLUMNS = {
    _TASK_DEDUPE_TABLE: {
        "idempotency_key",
        "request_hash",
        "task_id",
        "status",
        "request_payload_json",
        "result_json",
        "created_at",
        "updated_at",
        "expires_at",
        "completed_at",
    },
    _TASK_LEASE_TABLE: {
        "task_id",
        "executor_id",
        "attempt_number",
        "created_at",
        "updated_at",
        "started_at",
        "heartbeat_at",
        "expires_at",
        "lease_expires_at",
    },
    _TASK_LOCK_TABLE: {
        "lock_key",
        "owner_id",
        "created_at",
        "updated_at",
        "expires_at",
    },
}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    txt = str(value or "").strip()
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(txt, fmt)
        except Exception:
            pass
    return None


def _json_dumps(data: Any) -> Optional[str]:
    if data is None:
        return None
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    except Exception:
        try:
            return json.dumps({"raw": str(data)}, ensure_ascii=False, sort_keys=True)
        except Exception:
            return None


def _task_dedupe_window_seconds() -> int:
    raw = str(__import__("os").getenv("YOUTUBE_VIDEO_DEDUPE_WINDOW_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 6 * 60 * 60
    except Exception:
        value = 6 * 60 * 60
    return max(60, min(7 * 24 * 60 * 60, value))


def _task_dedupe_expires_at(now: Optional[datetime] = None) -> datetime:
    base = now or _utcnow()
    return base + timedelta(seconds=_task_dedupe_window_seconds())


def _table_column_names(db, table_name: str) -> set:
    try:
        return {str(col.get("name") or "").strip().lower() for col in inspect(db.bind).get_columns(table_name)}
    except Exception:
        return set()


def _ensure_task_support_tables():
    global _task_schema_ready
    if _task_schema_ready:
        return
    with _task_schema_lock:
        if _task_schema_ready:
            return
        db = SessionLocal()
        try:
            inspector = inspect(db.bind)
            available_tables = set(inspector.get_table_names())
            missing_tables = []
            missing_columns = {}

            for table_name, expected_columns in _TASK_SCHEMA_COLUMNS.items():
                if table_name not in available_tables:
                    missing_tables.append(table_name)
                    continue
                table_columns = _table_column_names(db, table_name)
                missing = sorted(col for col in expected_columns if col not in table_columns)
                if missing:
                    missing_columns[table_name] = missing

            if missing_tables or missing_columns:
                details = []
                if missing_tables:
                    details.append(f"tabelas ausentes: {', '.join(sorted(missing_tables))}")
                if missing_columns:
                    col_details = ", ".join(
                        f"{table} -> {', '.join(cols)}" for table, cols in sorted(missing_columns.items())
                    )
                    details.append(f"colunas ausentes: {col_details}")
                raise RuntimeError(
                    "Schema de idempotencia de video ausente ou incompleto. "
                    f"Detalhes: {'; '.join(details)}. "
                    f"Aplique a migration Alembic {_TASK_SCHEMA_REQUIRED_REVISION} com `alembic upgrade head` "
                    "no mesmo PostgreSQL usado pela aplicacao."
                )
            _task_schema_ready = True
        finally:
            db.close()


def _acquire_db_lock(lock_key: str, timeout_seconds: int = 15, ttl_seconds: int = 30) -> Dict[str, Any]:
    _ensure_task_support_tables()
    owner_id = str(uuid.uuid4())
    deadline = time.time() + max(1, int(timeout_seconds or 15))
    ttl = max(5, int(ttl_seconds or 30))
    while time.time() <= deadline:
        now = _utcnow()
        expires_at = now + timedelta(seconds=ttl)
        db = SessionLocal()
        try:
            try:
                db.execute(text(
                    f"""
                    INSERT INTO {_TASK_LOCK_TABLE} (lock_key, owner_id, expires_at, created_at, updated_at)
                    VALUES (:lock_key, :owner_id, :expires_at, :created_at, :updated_at)
                    """
                ), {
                    "lock_key": lock_key,
                    "owner_id": owner_id,
                    "expires_at": expires_at,
                    "created_at": now,
                    "updated_at": now,
                })
                db.commit()
                return {
                    "backend": "db",
                    "lock_key": lock_key,
                    "owner_id": owner_id,
                }
            except Exception:
                db.rollback()
            row = db.execute(text(
                f"SELECT owner_id, expires_at FROM {_TASK_LOCK_TABLE} WHERE lock_key = :lock_key"
            ), {"lock_key": lock_key}).mappings().first()
            if row:
                row_exp = _parse_dt(row.get("expires_at"))
                if row_exp and row_exp <= now:
                    try:
                        db.execute(text(
                            f"""
                            DELETE FROM {_TASK_LOCK_TABLE}
                            WHERE lock_key = :lock_key AND expires_at <= :now
                            """
                        ), {"lock_key": lock_key, "now": now})
                        db.commit()
                        continue
                    except Exception:
                        db.rollback()
        finally:
            db.close()
        time.sleep(0.12)
    raise TimeoutError(f"Timeout ao adquirir lock persistente: {lock_key}")


def _release_db_lock(lock_info: Dict[str, Any]):
    if not isinstance(lock_info, dict):
        return
    lock_key = str(lock_info.get("lock_key") or "").strip()
    owner_id = str(lock_info.get("owner_id") or "").strip()
    if not lock_key or not owner_id:
        return
    db = SessionLocal()
    try:
        db.execute(text(
            f"""
            DELETE FROM {_TASK_LOCK_TABLE}
            WHERE lock_key = :lock_key AND owner_id = :owner_id
            """
        ), {"lock_key": lock_key, "owner_id": owner_id})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def acquire_distributed_lock(lock_key: str, timeout_seconds: int = 15, ttl_seconds: int = 30) -> Dict[str, Any]:
    if _redis_conn is not None:
        try:
            lock = _redis_conn.lock(_LOCK_PREFIX + lock_key, timeout=max(5, int(ttl_seconds or 30)), blocking_timeout=max(1, int(timeout_seconds or 15)))
            if lock.acquire(blocking=True):
                return {"backend": "redis", "lock": lock, "lock_key": lock_key}
        except Exception:
            pass
    return _acquire_db_lock(lock_key=lock_key, timeout_seconds=timeout_seconds, ttl_seconds=ttl_seconds)


def release_distributed_lock(lock_info: Optional[Dict[str, Any]]):
    if not isinstance(lock_info, dict):
        return
    if lock_info.get("backend") == "redis":
        lock = lock_info.get("lock")
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        return
    _release_db_lock(lock_info)


def _fetch_dedupe_row(db, idempotency_key: str) -> Optional[Dict[str, Any]]:
    row = db.execute(text(
        f"""
        SELECT idempotency_key, request_hash, task_id, status, request_payload_json,
               result_json, created_at, updated_at, expires_at, completed_at
        FROM {_TASK_DEDUPE_TABLE}
        WHERE idempotency_key = :idempotency_key
        """
    ), {"idempotency_key": idempotency_key}).mappings().first()
    return dict(row) if row else None


def _fetch_dedupe_row_by_task_id(db, task_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute(text(
        f"""
        SELECT idempotency_key, request_hash, task_id, status, request_payload_json,
               result_json, created_at, updated_at, expires_at, completed_at
        FROM {_TASK_DEDUPE_TABLE}
        WHERE task_id = :task_id
        """
    ), {"task_id": task_id}).mappings().first()
    return dict(row) if row else None


def _task_result_payload_from_row(row: VideoTask) -> Dict[str, Any]:
    if not row or not getattr(row, "result_json", None):
        return {}
    try:
        result = json.loads(row.result_json)
    except Exception:
        return {}
    if not isinstance(result, dict):
        return {}
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}


def _find_equivalent_task_by_content_fingerprint(
    db,
    *,
    payload: Dict[str, Any],
    statuses: Optional[set] = None,
    limit: int = 400,
) -> Optional[VideoTask]:
    desired = str((payload or {}).get("content_fingerprint") or "").strip()
    if not desired:
        return None
    status_filter = statuses or {"pending", "processing", "completed"}
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.status.in_(list(status_filter)))
        .order_by(VideoTask.updated_at.desc(), VideoTask.created_at.desc())
        .limit(max(1, int(limit or 400)))
        .all()
    )
    for row in rows:
        task_payload = _task_result_payload_from_row(row)
        if str(task_payload.get("content_fingerprint") or "").strip() == desired:
            return row
    return None


def _fetch_lease_row(db, task_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute(text(
        f"""
        SELECT task_id, executor_id, attempt_number, created_at, updated_at,
               started_at, heartbeat_at,
               COALESCE(expires_at, lease_expires_at) AS expires_at,
               lease_expires_at
        FROM {_TASK_LEASE_TABLE}
        WHERE task_id = :task_id
        """
    ), {"task_id": task_id}).mappings().first()
    return dict(row) if row else None


def _upsert_dedupe_row(
    db,
    *,
    idempotency_key: str,
    request_hash: str,
    task_id: str,
    status: str,
    payload: Optional[Dict[str, Any]] = None,
    result_json: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
):
    now = _utcnow()
    existing = _fetch_dedupe_row(db, idempotency_key)
    data = {
        "idempotency_key": idempotency_key,
        "request_hash": request_hash,
        "task_id": task_id,
        "status": status,
        "request_payload_json": _json_dumps(payload),
        "result_json": result_json,
        "created_at": existing.get("created_at") if existing else now,
        "updated_at": now,
        "expires_at": expires_at,
        "completed_at": completed_at,
    }
    if existing:
        db.execute(text(
            f"""
            UPDATE {_TASK_DEDUPE_TABLE}
            SET request_hash = :request_hash,
                task_id = :task_id,
                status = :status,
                request_payload_json = COALESCE(:request_payload_json, request_payload_json),
                result_json = COALESCE(:result_json, result_json),
                updated_at = :updated_at,
                expires_at = COALESCE(:expires_at, expires_at),
                completed_at = :completed_at
            WHERE idempotency_key = :idempotency_key
            """
        ), data)
    else:
        db.execute(text(
            f"""
            INSERT INTO {_TASK_DEDUPE_TABLE} (
                idempotency_key, request_hash, task_id, status,
                request_payload_json, result_json, created_at, updated_at, expires_at, completed_at
            ) VALUES (
                :idempotency_key, :request_hash, :task_id, :status,
                :request_payload_json, :result_json, :created_at, :updated_at, :expires_at, :completed_at
            )
            """
        ), data)


def _sync_task_aux_state(db, task_id: str, *, status: Optional[str], result_json: Optional[str]):
    _ensure_task_support_tables()
    dedupe_row = _fetch_dedupe_row_by_task_id(db, task_id)
    if not dedupe_row:
        return
    status_norm = str(status or dedupe_row.get("status") or "").strip().lower()
    now = _utcnow()
    completed_at = now if status_norm == "completed" else None
    expires_at = _task_dedupe_expires_at(now) if status_norm in {"pending", "processing", "completed"} else now
    db.execute(text(
        f"""
        UPDATE {_TASK_DEDUPE_TABLE}
        SET status = :status,
            result_json = COALESCE(:result_json, result_json),
            updated_at = :updated_at,
            expires_at = :expires_at,
            completed_at = CASE WHEN :completed_at IS NOT NULL THEN :completed_at ELSE completed_at END
        WHERE task_id = :task_id
        """
    ), {
        "status": status_norm or "pending",
        "result_json": result_json,
        "updated_at": now,
        "expires_at": expires_at,
        "completed_at": completed_at,
        "task_id": task_id,
    })


def _task_aux_meta(db, task_id: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not task_id:
        return out
    _ensure_task_support_tables()
    dedupe = _fetch_dedupe_row_by_task_id(db, task_id)
    if dedupe:
        out["idempotency_key"] = dedupe.get("idempotency_key")
        out["request_hash"] = dedupe.get("request_hash")
        out["dedupe_status"] = dedupe.get("status")
        if dedupe.get("expires_at"):
            dt = _parse_dt(dedupe.get("expires_at"))
            out["dedupe_expires_at"] = dt.isoformat() if dt else dedupe.get("expires_at")
        if dedupe.get("completed_at"):
            dt = _parse_dt(dedupe.get("completed_at"))
            out["dedupe_completed_at"] = dt.isoformat() if dt else dedupe.get("completed_at")
    lease = _fetch_lease_row(db, task_id)
    if lease:
        out["executor_id"] = lease.get("executor_id")
        out["attempt_number"] = int(lease.get("attempt_number") or 1)
        for field, target in (
            ("created_at", "executor_created_at"),
            ("updated_at", "executor_updated_at"),
            ("started_at", "executor_started_at"),
            ("heartbeat_at", "executor_heartbeat_at"),
            ("expires_at", "executor_lease_expires_at"),
        ):
            dt = _parse_dt(lease.get(field))
            if dt:
                out[target] = dt.isoformat()
    return out

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
        _ensure_task_support_tables()
        row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if not row:
            return None
        row.status = "cancelled"
        row.message = message
        _sync_task_aux_state(db, task_id, status="cancelled", result_json=row.result_json)
        db.commit()
        current = _db_to_dict(row, aux_meta=_task_aux_meta(db, task_id))
        video_tasks[task_id] = current
        _redis_set(task_id, current)
        return current
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()


def reset_task_for_retry(task_id: str, progress: int = 1, message: str = "Reiniciando tarefa...") -> Optional[Dict[str, Any]]:
    _control_set(task_id, {"cancel": False, "deleted": False})
    db = SessionLocal()
    try:
        _ensure_task_support_tables()
        db.execute(text(
            f"""
            DELETE FROM {_TASK_LEASE_TABLE}
            WHERE task_id = :task_id
            """
        ), {"task_id": task_id})
        row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if not row:
            return None
        row.status = "processing"
        try:
            row.progress = int(progress)
        except Exception:
            row.progress = 1
        row.message = message
        _sync_task_aux_state(db, task_id, status="processing", result_json=row.result_json)
        db.commit()
        current = _db_to_dict(row, aux_meta=_task_aux_meta(db, task_id))
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

def _db_to_dict(row: VideoTask, aux_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = None
    if row.result_json:
        try:
            result = json.loads(row.result_json)
        except Exception:
            result = row.result_json
    out = {
        "task_id": row.id,
        "status": row.status,
        "progress": int(row.progress or 0),
        "message": row.message,
        "result": result,
        "created_at": (row.created_at.isoformat() if getattr(row, "created_at", None) else None),
        "updated_at": (row.updated_at.isoformat() if getattr(row, "updated_at", None) else None),
    }
    if aux_meta:
        out.update(aux_meta)
    return out

def create_task(
    user_id: Optional[int] = None,
    *,
    task_id: Optional[str] = None,
    initial_status: str = "pending",
    progress: int = 0,
    message: str = "Aguardando início...",
    result: Any = None,
):
    _ensure_task_support_tables()
    task_id = str(task_id or uuid.uuid4())
    initial = {
        "task_id": task_id,
        "status": initial_status,
        "progress": int(progress or 0),
        "message": message,
        "result": result,
        "created_at": _utcnow().isoformat(),
    }
    db = SessionLocal()
    try:
        existing = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if existing:
            current = _db_to_dict(existing, aux_meta=_task_aux_meta(db, task_id))
            video_tasks[task_id] = current
            _redis_set(task_id, current)
            return task_id
        row = VideoTask(
            id=task_id,
            user_id=user_id,
            status=initial_status,
            progress=int(progress or 0),
            message=message,
            result_json=_json_dumps(result),
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
        _ensure_task_support_tables()
        # #region debug-point D:task-progress-persist
        def _dbg_task_event(hypothesis_id, msg, data=None):
            try:
                import json as _json
                import urllib.request as _urlreq
                _p = ".dbg/render-stuck-86.env"
                _u, _s = "http://127.0.0.1:7777/event", "render-stuck-86"
                if not os.path.exists(_p):
                    return
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
                _urlreq.urlopen(_req, timeout=0.1).read()
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
        _sync_task_aux_state(db, task_id, status=(status or row.status), result_json=row.result_json)
        db.commit()
        current = _db_to_dict(row, aux_meta=_task_aux_meta(db, task_id))
        video_tasks[task_id] = current
        _redis_set(task_id, current)
        return current
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
        _ensure_task_support_tables()
        row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if row:
            current = _db_to_dict(row, aux_meta=_task_aux_meta(db, task_id))
            video_tasks[task_id] = current
            _redis_set(task_id, current)
            return current
    except Exception:
        pass
    finally:
        db.close()
    return video_tasks.get(task_id) or _redis_get(task_id)


def get_task_by_idempotency_key(idempotency_key: str) -> Optional[Dict[str, Any]]:
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    _ensure_task_support_tables()
    db = SessionLocal()
    try:
        row = _fetch_dedupe_row(db, key)
        if not row or not row.get("task_id"):
            return None
        task = db.query(VideoTask).filter(VideoTask.id == str(row.get("task_id"))).first()
        if not task:
            return None
        current = _db_to_dict(task, aux_meta=_task_aux_meta(db, task.id))
        video_tasks[task.id] = current
        _redis_set(task.id, current)
        return current
    except Exception:
        return None
    finally:
        db.close()


def claim_video_task(
    *,
    idempotency_key: str,
    request_hash: str,
    payload: Dict[str, Any],
    dedupe_window_seconds: int,
    force_regenerate: bool = False,
    user_id: Optional[int] = None,
    initial_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    key = str(idempotency_key or "").strip()
    req_hash = str(request_hash or "").strip()
    if not key or not req_hash:
        raise ValueError("idempotency_key e request_hash são obrigatórios.")
    _ensure_task_support_tables()
    lock_info = acquire_distributed_lock(f"claim:{key}", timeout_seconds=20, ttl_seconds=30)
    try:
        db = SessionLocal()
        try:
            now = _utcnow()
            dedupe = _fetch_dedupe_row(db, key)
            if dedupe and dedupe.get("task_id"):
                task_id = str(dedupe.get("task_id") or "").strip()
                task = db.query(VideoTask).filter(VideoTask.id == task_id).first()
                if task:
                    existing_user_id = getattr(task, "user_id", None)
                    if user_id is not None and existing_user_id not in (None, int(user_id)):
                        task = None
                    elif user_id is not None and existing_user_id is None:
                        task.user_id = int(user_id)
                        db.commit()
                if task:
                    status_norm = str(task.status or "").strip().lower()
                    dedupe_exp = _parse_dt(dedupe.get("expires_at"))
                    completed_at = _parse_dt(dedupe.get("completed_at")) or _parse_dt(getattr(task, "updated_at", None))
                    within_window = bool(
                        (dedupe_exp and dedupe_exp > now) or (
                            completed_at and
                            (now - completed_at) <= timedelta(seconds=max(60, int(dedupe_window_seconds or 21600)))
                        )
                    )
                    if not force_regenerate and status_norm in {"pending", "processing"}:
                        current = _db_to_dict(task, aux_meta=_task_aux_meta(db, task_id))
                        video_tasks[task_id] = current
                        _redis_set(task_id, current)
                        return {
                            "task_id": task_id,
                            "created_new_task": False,
                            "reused_existing_task": True,
                            "reused_completed_task": False,
                            "duplicate_prevented": True,
                            "matched_by": "idempotency_key",
                            "task": current,
                        }
                    if not force_regenerate and status_norm == "completed" and within_window:
                        current = _db_to_dict(task, aux_meta=_task_aux_meta(db, task_id))
                        video_tasks[task_id] = current
                        _redis_set(task_id, current)
                        return {
                            "task_id": task_id,
                            "created_new_task": False,
                            "reused_existing_task": True,
                            "reused_completed_task": True,
                            "duplicate_prevented": True,
                            "matched_by": "idempotency_key",
                            "task": current,
                        }
            equivalent_task = _find_equivalent_task_by_content_fingerprint(db, payload=payload)
            if equivalent_task and str(equivalent_task.id or "").strip():
                task_id = str(equivalent_task.id or "").strip()
                task = db.query(VideoTask).filter(VideoTask.id == task_id).first()
                if task and user_id is not None:
                    existing_user_id = getattr(task, "user_id", None)
                    if existing_user_id not in (None, int(user_id)):
                        task = None
                    elif existing_user_id is None:
                        task.user_id = int(user_id)
                        db.commit()
                if task and (force_regenerate is False):
                    status_norm = str(task.status or "").strip().lower()
                    task_payload = _task_result_payload_from_row(task)
                    _upsert_dedupe_row(
                        db,
                        idempotency_key=key,
                        request_hash=req_hash,
                        task_id=task_id,
                        status=status_norm or "pending",
                        payload=task_payload or payload,
                        result_json=getattr(task, "result_json", None),
                        expires_at=now + timedelta(seconds=max(60, int(dedupe_window_seconds or _task_dedupe_window_seconds()))),
                        completed_at=now if status_norm == "completed" else None,
                    )
                    db.commit()
                    current = _db_to_dict(task, aux_meta=_task_aux_meta(db, task_id))
                    video_tasks[task_id] = current
                    _redis_set(task_id, current)
                    return {
                        "task_id": task_id,
                        "created_new_task": False,
                        "reused_existing_task": True,
                        "reused_completed_task": bool(status_norm == "completed"),
                        "duplicate_prevented": True,
                        "matched_by": "content_fingerprint",
                        "task": current,
                    }
            task_payload = dict(initial_result or {})
            task_payload["payload"] = payload
            task_payload["idempotency_key"] = key
            task_payload["request_hash"] = req_hash
            task_id = create_task(
                user_id=user_id,
                initial_status="pending",
                progress=0,
                message="Aguardando início...",
                result=task_payload,
            )
            _upsert_dedupe_row(
                db,
                idempotency_key=key,
                request_hash=req_hash,
                task_id=task_id,
                status="pending",
                payload=payload,
                result_json=_json_dumps(task_payload),
                expires_at=now + timedelta(seconds=max(60, int(dedupe_window_seconds or _task_dedupe_window_seconds()))),
                completed_at=None,
            )
            db.commit()
            current = get_task(task_id) or {
                "task_id": task_id,
                "status": "pending",
                "progress": 0,
                "message": "Aguardando início...",
                "result": task_payload,
            }
            return {
                "task_id": task_id,
                "created_new_task": True,
                "reused_existing_task": False,
                "reused_completed_task": False,
                "duplicate_prevented": False,
                "matched_by": "new_task",
                "task": current,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    finally:
        release_distributed_lock(lock_info)


def acquire_task_execution_lease(task_id: str, executor_id: str, ttl_seconds: int = 180) -> Dict[str, Any]:
    tid = str(task_id or "").strip()
    owner = str(executor_id or "").strip()
    if not tid or not owner:
        return {"acquired": False}
    _ensure_task_support_tables()
    lock_info = acquire_distributed_lock(f"lease:{tid}", timeout_seconds=10, ttl_seconds=20)
    try:
        db = SessionLocal()
        try:
            now = _utcnow()
            expires_at = now + timedelta(seconds=max(30, int(ttl_seconds or 180)))
            row = _fetch_lease_row(db, tid)
            if row:
                lease_exp = _parse_dt(row.get("expires_at")) or _parse_dt(row.get("lease_expires_at"))
                active = bool(lease_exp and lease_exp > now)
                row_owner = str(row.get("executor_id") or "").strip()
                if active and row_owner and row_owner != owner:
                    return {
                        "acquired": False,
                        "executor_id": row_owner,
                        "attempt_number": int(row.get("attempt_number") or 1),
                    }
                attempt_number = int(row.get("attempt_number") or 1)
                started_at = _parse_dt(row.get("started_at")) or now
                if row_owner != owner:
                    attempt_number += 1
                    started_at = now
                db.execute(text(
                    f"""
                    UPDATE {_TASK_LEASE_TABLE}
                    SET executor_id = :executor_id,
                        attempt_number = :attempt_number,
                        updated_at = :updated_at,
                        started_at = :started_at,
                        heartbeat_at = :heartbeat_at,
                        expires_at = :expires_at,
                        lease_expires_at = :lease_expires_at
                    WHERE task_id = :task_id
                    """
                ), {
                    "task_id": tid,
                    "executor_id": owner,
                    "attempt_number": max(1, attempt_number),
                    "updated_at": now,
                    "started_at": started_at,
                    "heartbeat_at": now,
                    "expires_at": expires_at,
                    "lease_expires_at": expires_at,
                })
            else:
                attempt_number = 1
                started_at = now
                db.execute(text(
                    f"""
                    INSERT INTO {_TASK_LEASE_TABLE} (
                        task_id, executor_id, attempt_number, created_at, updated_at,
                        started_at, heartbeat_at, expires_at, lease_expires_at
                    ) VALUES (
                        :task_id, :executor_id, :attempt_number, :created_at, :updated_at,
                        :started_at, :heartbeat_at, :expires_at, :lease_expires_at
                    )
                    """
                ), {
                    "task_id": tid,
                    "executor_id": owner,
                    "attempt_number": 1,
                    "created_at": now,
                    "updated_at": now,
                    "started_at": started_at,
                    "heartbeat_at": now,
                    "expires_at": expires_at,
                    "lease_expires_at": expires_at,
                })
            db.commit()
            return {
                "acquired": True,
                "executor_id": owner,
                "attempt_number": int(attempt_number),
                "started_at": started_at.isoformat(),
                "heartbeat_at": now.isoformat(),
                "lease_expires_at": expires_at.isoformat(),
            }
        except Exception:
            db.rollback()
            return {"acquired": False}
        finally:
            db.close()
    finally:
        release_distributed_lock(lock_info)


def heartbeat_task_execution_lease(task_id: str, executor_id: str, ttl_seconds: int = 180) -> bool:
    tid = str(task_id or "").strip()
    owner = str(executor_id or "").strip()
    if not tid or not owner:
        return False
    _ensure_task_support_tables()
    db = SessionLocal()
    try:
        now = _utcnow()
        expires_at = now + timedelta(seconds=max(30, int(ttl_seconds or 180)))
        updated = db.execute(text(
            f"""
            UPDATE {_TASK_LEASE_TABLE}
            SET updated_at = :updated_at,
                heartbeat_at = :heartbeat_at,
                expires_at = :expires_at,
                lease_expires_at = :lease_expires_at
            WHERE task_id = :task_id AND executor_id = :executor_id
            """
        ), {
            "task_id": tid,
            "executor_id": owner,
            "updated_at": now,
            "heartbeat_at": now,
            "expires_at": expires_at,
            "lease_expires_at": expires_at,
        })
        db.commit()
        return bool(getattr(updated, "rowcount", 0))
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def get_task_execution_lease(task_id: str) -> Optional[Dict[str, Any]]:
    tid = str(task_id or "").strip()
    if not tid:
        return None
    _ensure_task_support_tables()
    db = SessionLocal()
    try:
        row = _fetch_lease_row(db, tid)
        if not row:
            return None
        out = dict(row)
        for field in ("created_at", "updated_at", "started_at", "heartbeat_at", "expires_at", "lease_expires_at"):
            dt = _parse_dt(out.get(field))
            if dt:
                out[field] = dt.isoformat()
        return out
    except Exception:
        return None
    finally:
        db.close()


def release_task_execution_lease(task_id: str, executor_id: str):
    tid = str(task_id or "").strip()
    owner = str(executor_id or "").strip()
    if not tid or not owner:
        return
    _ensure_task_support_tables()
    db = SessionLocal()
    try:
        db.execute(text(
            f"""
            DELETE FROM {_TASK_LEASE_TABLE}
            WHERE task_id = :task_id AND executor_id = :executor_id
            """
        ), {"task_id": tid, "executor_id": owner})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def finalize_task_once(task_id: str, *, status: str, progress: int, message: str, result: Any) -> Dict[str, Any]:
    tid = str(task_id or "").strip()
    if not tid:
        return {"finalized_now": False, "task": None}
    _ensure_task_support_tables()
    lock_info = acquire_distributed_lock(f"finalize:{tid}", timeout_seconds=15, ttl_seconds=20)
    try:
        db = SessionLocal()
        try:
            row = db.query(VideoTask).filter(VideoTask.id == tid).first()
            if row and str(row.status or "").strip().lower() == str(status or "").strip().lower():
                current = _db_to_dict(row, aux_meta=_task_aux_meta(db, tid))
                video_tasks[tid] = current
                _redis_set(tid, current)
                return {"finalized_now": False, "task": current}
            if not row:
                row = VideoTask(
                    id=tid,
                    status=status,
                    progress=int(progress or 0),
                    message=message,
                    result_json=_json_dumps(result),
                )
                db.add(row)
            else:
                row.status = status
                row.progress = int(progress or 0)
                row.message = message
                row.result_json = _json_dumps(result)
            _sync_task_aux_state(db, tid, status=status, result_json=row.result_json)
            db.commit()
            db.refresh(row)
            current = _db_to_dict(row, aux_meta=_task_aux_meta(db, tid))
            video_tasks[tid] = current
            _redis_set(tid, current)
            return {"finalized_now": True, "task": current}
        except Exception:
            db.rollback()
            return {"finalized_now": False, "task": get_task(tid)}
        finally:
            db.close()
    finally:
        release_distributed_lock(lock_info)
