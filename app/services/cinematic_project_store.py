from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CinematicProjectStore:
    """Durable per-user store for the active cinematic production.

    The Codexia production shell is intentionally lightweight. This store keeps
    the expensive/recoverable state (Claude plan, estimate, provider jobs and
    approved pilot URLs) on the persistent /data volume so browser refreshes,
    phone changes and short network losses do not force a paid regeneration.
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

    def _path(self, user_id: int) -> Path:
        return self.root / f"u{max(0, int(user_id or 0))}-active.json"

    def read(self, user_id: int) -> Optional[Dict[str, Any]]:
        path = self._path(user_id)
        with self._lock:
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) else None

    def write(self, user_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            current = self.read(user_id) or {
                "version": 1,
                "user_id": int(user_id or 0),
                "project_id": "active",
                "created_at": _utcnow(),
                "scenes": {},
            }
            for key, value in dict(payload or {}).items():
                if key in {"user_id", "project_id", "created_at", "version"}:
                    continue
                current[key] = value
            current["version"] = 1
            current["user_id"] = int(user_id or 0)
            current["project_id"] = "active"
            current.setdefault("created_at", _utcnow())
            if not isinstance(current.get("scenes"), dict):
                current["scenes"] = {}
            current["updated_at"] = _utcnow()
            path = self._path(user_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return current

    def update_scene(self, user_id: int, scene_index: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        index = max(1, int(scene_index))
        with self._lock:
            current = self.read(user_id) or self.write(user_id, {})
            scenes = current.get("scenes") if isinstance(current.get("scenes"), dict) else {}
            key = str(index)
            scene = scenes.get(key) if isinstance(scenes.get(key), dict) else {}
            scene.update(dict(payload or {}))
            scene["scene_index"] = index
            scene["updated_at"] = _utcnow()
            scenes[key] = scene
            current["scenes"] = scenes
            return self.write(user_id, current)

    def clear(self, user_id: int) -> bool:
        path = self._path(user_id)
        with self._lock:
            existed = path.exists()
            path.unlink(missing_ok=True)
            return existed
