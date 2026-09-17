from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slot(value: str | None) -> str:
    value = str(value or "story").strip().lower()
    return value if value in {"story", "devotional", "short"} else "story"


class CinematicProjectStore:
    """Durable per-user store with isolated production slots.

    `story` keeps the legacy active-project filename for backwards compatibility.
    Devotionals and Shorts use their own files so one production cannot overwrite
    or visually bleed into another while both are being worked on in parallel.
    """

    def __init__(self, root: Optional[str | Path] = None):
        if root is None:
            root = (
                Path("/data/codexia/cinematic/projects")
                if os.path.isdir("/data")
                else Path(".codexia/cinematic/projects")
            )
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, user_id: int, slot: str = "story") -> Path:
        safe_slot = _slot(slot)
        suffix = "active" if safe_slot == "story" else safe_slot
        return self.root / f"u{max(0, int(user_id or 0))}-{suffix}.json"

    def read(self, user_id: int, slot: str = "story") -> Optional[Dict[str, Any]]:
        path = self._path(user_id, slot)
        with self._lock:
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) else None

    def write(self, user_id: int, payload: Dict[str, Any], slot: str = "story") -> Dict[str, Any]:
        payload = dict(payload or {})
        # Some background producers predate project slots and call write() without
        # passing a slot explicitly. Never let a devotional/short fall back into
        # the legacy story file merely because the caller omitted that argument.
        requested_slot = _slot(slot)
        payload_slot = _slot(payload.get("content_type")) if payload.get("content_type") else None
        safe_slot = payload_slot if requested_slot == "story" and payload_slot in {"devotional", "short"} else requested_slot
        with self._lock:
            current = self.read(user_id, safe_slot) or {
                "version": 2,
                "user_id": int(user_id or 0),
                "project_id": safe_slot,
                "project_slot": safe_slot,
                "created_at": _utcnow(),
                "scenes": {},
            }
            for key, value in payload.items():
                if key in {"user_id", "project_id", "project_slot", "created_at", "version"}:
                    continue
                current[key] = value
            current["version"] = 2
            current["user_id"] = int(user_id or 0)
            current["project_id"] = safe_slot
            current["project_slot"] = safe_slot
            current.setdefault("created_at", _utcnow())
            if not isinstance(current.get("scenes"), dict):
                current["scenes"] = {}
            current["updated_at"] = _utcnow()
            path = self._path(user_id, safe_slot)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return current

    def update_scene(
        self,
        user_id: int,
        scene_index: int,
        payload: Dict[str, Any],
        slot: str = "story",
    ) -> Dict[str, Any]:
        safe_slot = _slot(slot)
        index = max(1, int(scene_index))
        with self._lock:
            current = self.read(user_id, safe_slot) or self.write(user_id, {}, safe_slot)
            scenes = current.get("scenes") if isinstance(current.get("scenes"), dict) else {}
            key = str(index)
            scene = scenes.get(key) if isinstance(scenes.get(key), dict) else {}
            scene.update(dict(payload or {}))
            scene["scene_index"] = index
            scene["updated_at"] = _utcnow()
            scenes[key] = scene
            current["scenes"] = scenes
            return self.write(user_id, current, safe_slot)

    def clear(self, user_id: int, slot: str = "story") -> bool:
        path = self._path(user_id, slot)
        with self._lock:
            existed = path.exists()
            path.unlink(missing_ok=True)
            return existed
