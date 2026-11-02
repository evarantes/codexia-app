"""Live resource snapshots for the Codexia app server and CX33 worker."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.services.video_resource_guard import capture_resource_snapshot

WORKER_RESOURCES_KEY = "codexia:worker:resources:v1"
WORKER_RESOURCES_TTL_SECONDS = 120
DRIVE_USAGE_CACHE_SECONDS = 300

_DRIVE_CACHE: Dict[int, tuple[float, Dict[str, Any]]] = {}
_DRIVE_REFRESHING: set[int] = set()
_DRIVE_CACHE_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _storage_paths() -> list[tuple[str, str]]:
    paths = [("Sistema", "/"), ("Dados", "/data")]
    try:
        from app.config import (
            AUDIO_OUTPUT_DIR,
            BOOKS_OUTPUT_DIR,
            BRANDING_OUTPUT_DIR,
            COVERS_OUTPUT_DIR,
            IMAGES_OUTPUT_DIR,
            MUSIC_OUTPUT_DIR,
            VIDEO_OUTPUT_DIR,
        )

        paths.extend([
            ("Vídeos", VIDEO_OUTPUT_DIR),
            ("Áudios", AUDIO_OUTPUT_DIR),
            ("Imagens", IMAGES_OUTPUT_DIR),
            ("Músicas", MUSIC_OUTPUT_DIR),
            ("Livros", BOOKS_OUTPUT_DIR),
            ("Capas", COVERS_OUTPUT_DIR),
            ("Identidade visual", BRANDING_OUTPUT_DIR),
        ])
    except Exception:
        pass
    return paths


def _storage_snapshots() -> list[Dict[str, Any]]:
    by_device: Dict[int, Dict[str, Any]] = {}
    for label, raw_path in _storage_paths():
        path = str(raw_path or "/")
        try:
            stat = os.stat(path)
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        device = int(stat.st_dev)
        item = by_device.get(device)
        if item is None:
            total = int(usage.total)
            used = int(usage.used)
            free = int(usage.free)
            item = {
                "areas": [],
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_percent": round((used / total * 100) if total else 0.0, 1),
            }
            by_device[device] = item
        if label not in item["areas"]:
            item["areas"].append(label)
        if path not in item.get("mounts", []):
            item.setdefault("mounts", []).append(path)
    return list(by_device.values())


def capture_node_resources() -> Dict[str, Any]:
    snapshot = capture_resource_snapshot(disk_path="/")
    total_memory = float(snapshot.get("total_memory_mb") or 0.0)
    available_memory = min(total_memory, float(snapshot.get("available_memory_mb") or 0.0))
    used_memory = max(0.0, total_memory - available_memory)
    swap_total = float(snapshot.get("swap_total_mb") or 0.0)
    swap_percent = float(snapshot.get("swap_used_percent") or 0.0)
    return {
        "captured_at": str(snapshot.get("captured_at") or _utc_now()),
        "memory": {
            "total_mb": round(total_memory, 1),
            "available_mb": round(available_memory, 1),
            "used_mb": round(used_memory, 1),
            "used_percent": round((used_memory / total_memory * 100) if total_memory else 0.0, 1),
        },
        "swap": {
            "total_mb": round(swap_total, 1),
            "used_percent": round(swap_percent, 1),
            "used_mb": round(swap_total * swap_percent / 100, 1),
        },
        "cpu": {
            "logical_cores": int(snapshot.get("cpu_count") or 0),
            "load_1m": round(float(snapshot.get("load_1m") or 0.0), 2),
            "load_per_core": round(float(snapshot.get("load_per_cpu") or 0.0), 2),
        },
        "process": {
            "memory_mb": round(float(snapshot.get("process_rss_mb") or 0.0), 1),
            "peak_memory_mb": round(float(snapshot.get("process_peak_rss_mb") or 0.0), 1),
        },
        "storage": _storage_snapshots(),
    }


def publish_worker_resources(
    connection: Any,
    *,
    resources: Optional[Dict[str, Any]] = None,
    ttl_seconds: int = WORKER_RESOURCES_TTL_SECONDS,
) -> Dict[str, Any]:
    if connection is None:
        return {}
    payload = {
        "protocol_version": 1,
        "captured_at": _utc_now(),
        "resources": resources if isinstance(resources, dict) else capture_node_resources(),
    }
    connection.setex(
        WORKER_RESOURCES_KEY,
        max(30, min(600, int(ttl_seconds))),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    return payload


def read_worker_resources(connection: Any) -> Dict[str, Any]:
    if connection is None:
        return {}
    try:
        raw = connection.get(WORKER_RESOURCES_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(str(raw or ""))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _refresh_drive_usage(user_id: int) -> None:
    try:
        from app.services.google_drive_archive import GoogleDriveArchive

        result = GoogleDriveArchive.archive_usage_all(user_id=user_id)
    except Exception:
        result = {"status": "error", "connected": False, "error": "Não foi possível consultar o arquivo do Drive."}
    with _DRIVE_CACHE_LOCK:
        _DRIVE_CACHE[user_id] = (time.monotonic() + DRIVE_USAGE_CACHE_SECONDS, result)
        _DRIVE_REFRESHING.discard(user_id)


def invalidate_drive_usage_cache(user_id: Optional[int] = None) -> None:
    with _DRIVE_CACHE_LOCK:
        if user_id is None:
            _DRIVE_CACHE.clear()
        else:
            _DRIVE_CACHE.pop(int(user_id), None)


def _drive_usage(user_id: int) -> Dict[str, Any]:
    now = time.monotonic()
    with _DRIVE_CACHE_LOCK:
        cached = _DRIVE_CACHE.get(user_id)
        if cached and cached[0] > now:
            return dict(cached[1])
        if user_id not in _DRIVE_REFRESHING:
            _DRIVE_REFRESHING.add(user_id)
            threading.Thread(
                target=_refresh_drive_usage,
                args=(user_id,),
                name="drive-archive-usage",
                daemon=True,
            ).start()
        if cached:
            stale = dict(cached[1])
            stale["stale"] = True
            return stale
    return {"status": "checking", "connected": None, "archive_files": 0, "archive_bytes": 0}


def server_resources_overview(connection: Any, *, admin_user_id: int) -> Dict[str, Any]:
    try:
        principal = {"status": "online", "resources": capture_node_resources()}
    except Exception:
        principal = {"status": "error", "resources": {}}

    worker_payload = read_worker_resources(connection)
    worker_resources = worker_payload.get("resources") if isinstance(worker_payload.get("resources"), dict) else {}
    try:
        from app.services.worker_release_health import read_worker_release

        worker_release = read_worker_release(connection)
    except Exception:
        worker_release = {}
    worker = {
        "status": "online" if worker_resources else ("starting" if worker_release else "offline"),
        "captured_at": str(worker_payload.get("captured_at") or ""),
        "resources": worker_resources,
    }
    return {
        "captured_at": _utc_now(),
        "refresh_seconds": 30,
        "servers": {"principal": principal, "cx33": worker},
        "google_drive": _drive_usage(int(admin_user_id)),
    }


__all__ = [
    "WORKER_RESOURCES_KEY",
    "capture_node_resources",
    "invalidate_drive_usage_cache",
    "publish_worker_resources",
    "read_worker_resources",
    "server_resources_overview",
]
