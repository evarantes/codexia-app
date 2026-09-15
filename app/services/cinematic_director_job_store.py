from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,120}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DirectorJobStore:
    """Small durable JSON store for long-running cinematic director requests.

    The browser generates the request/job id before submitting. Repeating the
    same id is idempotent, so a mobile connection loss cannot accidentally bill
    Claude twice. Files live on the persistent /data volume in production.
    """

    def __init__(self, root: Optional[str | Path] = None):
        if root is None:
            root = (
                Path("/data/codexia/cinematic/director-jobs")
                if os.path.isdir("/data")
                else Path(".codexia/cinematic/director-jobs")
            )
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def validate_job_id(job_id: str) -> str:
        value = str(job_id or "").strip()
        if not _JOB_ID_RE.fullmatch(value):
            raise ValueError("request_id inválido")
        return value

    def _path(self, user_id: int, job_id: str) -> Path:
        safe = self.validate_job_id(job_id)
        return self.root / f"u{max(0, int(user_id or 0))}-{safe}.json"

    def read(self, user_id: int, job_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(user_id, job_id)
        with self._lock:
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) else None

    def _write(self, user_id: int, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        path = self._path(user_id, job_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return payload

    def create_if_absent(
        self,
        user_id: int,
        job_id: str,
        request_payload: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool]:
        safe = self.validate_job_id(job_id)
        with self._lock:
            existing = self.read(user_id, safe)
            if existing is not None:
                return existing, False
            now = _utcnow()
            payload: Dict[str, Any] = {
                "job_id": safe,
                "user_id": int(user_id or 0),
                "status": "queued",
                "stage": "queued",
                "progress": 0,
                "message": "Direção recebida. Claude entrará em execução em segundo plano.",
                "request": dict(request_payload or {}),
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            return self._write(user_id, safe, payload), True

    def update(self, user_id: int, job_id: str, **changes: Any) -> Dict[str, Any]:
        safe = self.validate_job_id(job_id)
        with self._lock:
            payload = self.read(user_id, safe)
            if payload is None:
                raise KeyError(safe)
            payload.update(changes)
            payload["updated_at"] = _utcnow()
            return self._write(user_id, safe, payload)

    def cleanup(self, *, older_than_days: int = 14) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(older_than_days)))
        removed = 0
        with self._lock:
            for path in self.root.glob("u*-*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if str(data.get("status") or "") not in {"completed", "failed"}:
                        continue
                    stamp = datetime.fromisoformat(str(data.get("updated_at") or "").replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    if stamp < cutoff:
                        path.unlink(missing_ok=True)
                        removed += 1
                except Exception:
                    continue
        return removed
