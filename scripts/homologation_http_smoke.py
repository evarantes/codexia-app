import os
import sys
from typing import Dict, List, Tuple

import requests


def _get_token(base_url: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{base_url}/token",
        data={"username": username, "password": password},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token_status={resp.status_code}")
    token = (resp.json() or {}).get("access_token") or ""
    if not token:
        raise RuntimeError("token_missing")
    return token


def main() -> int:
    base_url = (os.getenv("CODEXIA_BASE_URL") or "http://127.0.0.1:8011").rstrip("/")
    username = os.getenv("CODEXIA_ADMIN_EMAIL") or "admin@codexia.dev"
    password = os.getenv("CODEXIA_ADMIN_PASSWORD") or "admin123"

    token = _get_token(base_url, username, password)
    auth_headers = {"Authorization": f"Bearer {token}"}

    public_endpoints: List[str] = [
        "/",
        "/docs",
        "/openapi.json",
        "/api/status",
        "/health",
    ]

    auth_endpoints: List[str] = [
        "/settings/",
        "/settings/provider-status",
        "/youtube/auth_url",
        "/youtube/auto/stats",
        "/youtube/auto/queue",
        "/youtube/guardian/overview",
        "/youtube/guardian/ledger",
        "/youtube/tasks/queue",
        "/youtube/videos",
        "/youtube/content-factory/drafts",
        "/bible-video-factory/bootstrap",
        "/bible-video-factory/dashboard",
        "/bible-video-factory/series",
        "/ai-factory/library",
        "/humor-factory/channel",
        "/humor-factory/projects",
        "/crm/customers",
        "/books/",
    ]

    results: List[Tuple[str, int]] = []

    for path in public_endpoints:
        resp = requests.get(f"{base_url}{path}", timeout=30)
        results.append((path, resp.status_code))

    for path in auth_endpoints:
        resp = requests.get(f"{base_url}{path}", headers=auth_headers, timeout=30)
        results.append((path, resp.status_code))

    ok = True
    for path, status in results:
        sys.stdout.write(f"{path} {status}\n")
        if status != 200:
            ok = False

    sys.stdout.write(f"ALL_200 {str(ok).upper()}\n")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

