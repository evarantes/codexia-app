from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional


WORKER_RELEASE_HEALTH_VERSION = 1
WORKER_RELEASE_KEY = "codexia:worker:release:v1"


def current_release_commit() -> str:
    """Return the immutable source revision injected by the deployment."""
    return str(
        os.getenv("RENDER_GIT_COMMIT")
        or os.getenv("SOURCE_COMMIT")
        or os.getenv("COMMIT_SHA")
        or os.getenv("GIT_COMMIT")
        or ""
    ).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name) or default).strip())
    except Exception:
        value = int(default)
    return max(minimum, min(maximum, value))


def build_worker_release_payload(
    *,
    commit: Optional[str] = None,
    started_at: Optional[str] = None,
) -> Dict[str, Any]:
    now = _utc_now()
    return {
        "protocol_version": WORKER_RELEASE_HEALTH_VERSION,
        "service": "rq-worker",
        "commit": str(commit if commit is not None else current_release_commit()).strip(),
        "started_at": str(started_at or now),
        "last_seen_at": now,
    }


def publish_worker_release(
    connection: Any,
    *,
    commit: Optional[str] = None,
    started_at: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    if connection is None:
        return {}
    ttl = int(
        ttl_seconds
        if ttl_seconds is not None
        else _bounded_int("WORKER_RELEASE_TTL_SECONDS", 120, 60, 600)
    )
    payload = build_worker_release_payload(commit=commit, started_at=started_at)
    connection.setex(
        WORKER_RELEASE_KEY,
        max(30, ttl),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    return payload


def read_worker_release(connection: Any) -> Dict[str, Any]:
    if connection is None:
        return {}
    try:
        raw = connection.get(WORKER_RELEASE_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(str(raw or ""))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def worker_release_health_snapshot(
    *,
    app_commit: Any,
    worker_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    payload = worker_payload if isinstance(worker_payload, dict) else {}
    app_revision = str(app_commit or "").strip()
    worker_revision = str(payload.get("commit") or "").strip()
    online = bool(payload)
    same_version = bool(app_revision and worker_revision and app_revision == worker_revision)
    if same_version:
        status = "ok"
    elif app_revision and worker_revision:
        status = "version_mismatch"
    else:
        status = "worker_unavailable"
    return {
        "status": status,
        "online": online,
        "same_version": same_version,
        "app_commit": app_revision,
        "worker_commit": worker_revision,
        "worker_last_seen_at": str(payload.get("last_seen_at") or ""),
        "worker_started_at": str(payload.get("started_at") or ""),
        "protocol_version": int(payload.get("protocol_version") or 0),
    }


def start_worker_release_heartbeat(connection: Any) -> threading.Thread:
    """Publish a short-lived release heartbeat for independent deploy checks."""
    commit = current_release_commit()
    started_at = _utc_now()
    ttl = _bounded_int("WORKER_RELEASE_TTL_SECONDS", 120, 60, 600)
    interval = _bounded_int(
        "WORKER_RELEASE_HEARTBEAT_SECONDS",
        30,
        10,
        max(10, ttl // 2),
    )

    def _pulse() -> None:
        while True:
            try:
                publish_worker_release(
                    connection,
                    commit=commit,
                    started_at=started_at,
                    ttl_seconds=ttl,
                )
            except Exception:
                pass
            threading.Event().wait(interval)

    thread = threading.Thread(
        target=_pulse,
        name="worker-release-heartbeat",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "WORKER_RELEASE_HEALTH_VERSION",
    "WORKER_RELEASE_KEY",
    "build_worker_release_payload",
    "current_release_commit",
    "publish_worker_release",
    "read_worker_release",
    "start_worker_release_heartbeat",
    "worker_release_health_snapshot",
]
