from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slot(value: Any) -> str:
    raw = str(value or "story").strip().lower()
    return raw if raw in {"story", "devotional", "short"} else "story"


class CinematicLibraryStore:
    """Durable index of projects explicitly created from the Codexia V2 UI.

    The canonical VideoTask/UnifiedVideo tables continue to own execution and
    audit data. This store only owns the V2 library membership and presentation
    metadata. That separation prevents tasks created by the legacy UI from
    leaking into the new interface and lets the V2 library implement a safe
    soft-delete without destroying pipeline audit records or media by accident.
    """

    def __init__(self, root: Optional[str | Path] = None):
        if root is None:
            root = (
                Path("/data/codexia/cinematic/library")
                if os.path.isdir("/data")
                else Path(".codexia/cinematic/library")
            )
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, user_id: int) -> Path:
        return self.root / f"u{max(0, int(user_id or 0))}.json"

    def _read_document(self, user_id: int) -> Dict[str, Any]:
        path = self._path(user_id)
        if not path.exists():
            return {
                "version": 1,
                "user_id": int(user_id or 0),
                "items": {},
                "updated_at": _utcnow(),
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        items = data.get("items") if isinstance(data.get("items"), dict) else {}
        return {
            "version": 1,
            "user_id": int(user_id or 0),
            "items": items,
            "updated_at": str(data.get("updated_at") or _utcnow()),
        }

    def _write_document(self, user_id: int, document: Dict[str, Any]) -> None:
        payload = dict(document or {})
        payload["version"] = 1
        payload["user_id"] = int(user_id or 0)
        payload["updated_at"] = _utcnow()
        if not isinstance(payload.get("items"), dict):
            payload["items"] = {}
        path = self._path(user_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def register(
        self,
        user_id: int,
        task_id: str,
        *,
        title: str = "",
        project_slot: str = "story",
        duration_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        task = str(task_id or "").strip()
        if not task:
            raise ValueError("task_id é obrigatório")
        uid = int(user_id or 0)
        with self._lock:
            doc = self._read_document(uid)
            items = doc["items"]
            current = items.get(task) if isinstance(items.get(task), dict) else {}
            current.update({
                "task_id": task,
                "origin": "cinematic_v2",
                "title": str(title or current.get("title") or "").strip()[:180],
                "project_slot": _slot(project_slot or current.get("project_slot")),
                "duration_minutes": (
                    int(duration_minutes)
                    if duration_minutes is not None
                    else current.get("duration_minutes")
                ),
                "deleted_at": None,
                "updated_at": _utcnow(),
            })
            current.setdefault("registered_at", _utcnow())
            items[task] = current
            self._write_document(uid, doc)
            return dict(current)

    def get(self, user_id: int, task_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        uid = int(user_id or 0)
        task = str(task_id or "").strip()
        with self._lock:
            doc = self._read_document(uid)
            value = doc["items"].get(task)
            if not isinstance(value, dict):
                return None
            if value.get("deleted_at") and not include_deleted:
                return None
            return dict(value)

    def list(self, user_id: int, *, include_deleted: bool = False) -> List[Dict[str, Any]]:
        uid = int(user_id or 0)
        with self._lock:
            doc = self._read_document(uid)
            values: List[Dict[str, Any]] = []
            for value in doc["items"].values():
                if not isinstance(value, dict):
                    continue
                if value.get("deleted_at") and not include_deleted:
                    continue
                values.append(dict(value))
            values.sort(
                key=lambda item: str(item.get("updated_at") or item.get("registered_at") or ""),
                reverse=True,
            )
            return values

    def soft_delete(self, user_id: int, task_id: str) -> bool:
        uid = int(user_id or 0)
        task = str(task_id or "").strip()
        with self._lock:
            doc = self._read_document(uid)
            current = doc["items"].get(task)
            if not isinstance(current, dict):
                return False
            current["deleted_at"] = _utcnow()
            current["updated_at"] = _utcnow()
            doc["items"][task] = current
            self._write_document(uid, doc)
            return True

    def restore(self, user_id: int, task_id: str) -> bool:
        uid = int(user_id or 0)
        task = str(task_id or "").strip()
        with self._lock:
            doc = self._read_document(uid)
            current = doc["items"].get(task)
            if not isinstance(current, dict):
                return False
            current["deleted_at"] = None
            current["updated_at"] = _utcnow()
            doc["items"][task] = current
            self._write_document(uid, doc)
            return True
