#!/usr/bin/env python3
"""Safe HTTP smoke for Entre Nós Dois in an isolated Codexia environment.

Read-only checks. No TTS, video, image upload, publish or paid-provider calls.
Usage: python scripts/entre_nos_dois_smoke.py --base-url http://127.0.0.1:8000
Admin checks: set CODEXIA_STUDIO_SMOKE_TOKEN in the shell and pass --admin-checks.
Never pass tokens on the command line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib import error, parse, request


def fetch(base: str, path: str, token: str | None = None) -> tuple[int, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(base.rstrip("/") + path, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=12) as response:
            return response.status, response.read(1024 * 1024).decode("utf-8", "replace")
    except error.HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", "replace")


def run(base_url: str, *, admin_token: str | None = None) -> dict:
    parsed = parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Use uma URL HTTP(S) sem usuário ou senha.")
    checks: list[dict] = []

    def check(name: str, path: str, allowed: tuple[int, ...], token: str | None = None):
        try:
            code, body = fetch(base_url, path, token)
            passed = code in allowed
            if passed and token and path.endswith("/overview"):
                data = json.loads(body)
                passed = data.get("program_title") is not None and data.get("auto_publish") is False
            if passed and token and path.endswith("/readiness"):
                data = json.loads(body)
                passed = data.get("paid_execution_enabled") is False and data.get("auto_publish") is False
            if passed and token and path.endswith("/references"):
                data = json.loads(body)
                passed = isinstance(data.get("hosts"), dict)
            checks.append({"check": name, "status": code, "passed": passed})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            checks.append({"check": name, "status": "network_or_response_error", "passed": False})

    check("page_html", "/studio/entre-nos-dois/painel", (200,))
    for name, path in (
        ("overview_requires_login", "/studio/entre-nos-dois/overview"),
        ("readiness_requires_login", "/studio/entre-nos-dois/readiness"),
        ("references_require_login", "/studio/entre-nos-dois/references"),
        ("private_image_requires_login", "/studio/entre-nos-dois/references/Helena/image"),
    ):
        check(name, path, (401, 403))
    if admin_token:
        check("admin_overview", "/studio/entre-nos-dois/overview", (200,), admin_token)
        check("admin_readiness", "/studio/entre-nos-dois/readiness", (200,), admin_token)
        # Storage not configured returns 503; it is a deployment blocker, not pass.
        check("admin_references", "/studio/entre-nos-dois/references", (200,), admin_token)
    return {
        "program": "entre-nos-dois",
        "all_passed": all(x["passed"] for x in checks),
        "checks": checks,
        "paid_calls_made": False,
        "assets_written": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--admin-checks", action="store_true")
    options = parser.parse_args()
    token = os.getenv("CODEXIA_STUDIO_SMOKE_TOKEN") if options.admin_checks else None
    if options.admin_checks and not token:
        parser.error("Defina CODEXIA_STUDIO_SMOKE_TOKEN no ambiente para os testes autenticados.")
    try:
        result = run(options.base_url, admin_token=token)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
