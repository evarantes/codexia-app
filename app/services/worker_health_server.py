"""Lightweight HTTP health endpoint for the RQ worker container.

The endpoint is intentionally served by the worker process itself.  A 200
response therefore proves the process is alive, can reach Redis, can query
PostgreSQL, and still has an active RQ registration.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from sqlalchemy import text

from app.database import engine


def _port() -> int:
    try:
        value = int(os.getenv("WORKER_HEALTH_PORT", "8081"))
    except ValueError:
        value = 8081
    return max(1024, min(value, 65535))


def build_health_snapshot(worker: Any, connection: Any) -> Dict[str, Any]:
    checks: Dict[str, Any] = {
        "process": {"ok": True, "pid": os.getpid()},
        "redis": {"ok": False},
        "postgres": {"ok": False},
        "rq": {"ok": False},
    }
    try:
        connection.ping()
        checks["redis"] = {"ok": True}
    except Exception as exc:
        checks["redis"] = {"ok": False, "error": type(exc).__name__}

    try:
        with engine.connect() as db_connection:
            db_connection.execute(text("SELECT 1"))
        checks["postgres"] = {"ok": True}
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": type(exc).__name__}

    try:
        # The worker key is the authoritative registration. During RQ startup
        # get_state() can briefly be a transitional value; the key exists only
        # while this live worker is registered and has a heartbeat TTL.
        state = str(worker.get_state() or "").lower()
        registered = bool(connection.exists(worker.key))
        checks["rq"] = {
            "ok": registered and state not in {"", "dead", "stopped"},
            "state": state,
            "name": str(getattr(worker, "name", "")),
            "registered": registered,
        }
    except Exception as exc:
        checks["rq"] = {"ok": False, "error": type(exc).__name__}

    ok = all(bool(item.get("ok")) for item in checks.values())
    return {"status": "ok" if ok else "error", "checks": checks}


def start_worker_health_server(worker: Any, connection: Any) -> threading.Thread:
    """Start the endpoint in a daemon thread and return it."""

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path.rstrip("/") not in {"", "/health", "/health/worker"}:
                self.send_error(404)
                return
            payload = build_health_snapshot(worker, connection)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if payload["status"] == "ok" else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", _port()), HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="worker-health-server",
        daemon=True,
    )
    thread.start()
    print(f"Worker health endpoint listening on :{_port()}/health")
    return thread


__all__ = ["build_health_snapshot", "start_worker_health_server"]
