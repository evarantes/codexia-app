#!/usr/bin/env python3
"""Real HTTP staging acceptance: login -> forced password rotation -> portrait upload.

Uses only disposable test credentials and a generated 1x1 PNG. No paid services.
Never run against production, real accounts or real portraits.
"""
from __future__ import annotations

import argparse
import json
import struct
import zlib
from urllib import error, parse, request


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def pixel_png() -> bytes:
    pixel = b"\x00\x60\x80\xa0"  # filter 0 + 3 RGB bytes
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(pixel))
            + _chunk(b"IEND", b""))


def call(base: str, path: str, *, method: str = "GET", token: str | None = None,
         data: bytes | None = None, content_type: str | None = None) -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if content_type:
        headers["Content-Type"] = content_type
    req = request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=12) as resp:
            return resp.status, resp.read(1024 * 1024)
    except error.HTTPError as exc:
        return exc.code, exc.read(4096)


def exercise(base: str, *, email: str, initial_password: str, next_password: str) -> dict:
    if not base.startswith("http://127.0.0.1:") and not base.startswith("http://localhost:"):
        raise ValueError("Teste E2E só aceita localhost.")
    if not all((email, initial_password, next_password)) or initial_password == next_password:
        raise ValueError("Credenciais de teste inválidas.")

    code, body = call(
        base, "/token", method="POST",
        data=parse.urlencode({"username": email, "password": initial_password}).encode(),
        content_type="application/x-www-form-urlencoded",
    )
    assert code == 200, "Login inicial de homologação falhou."
    login = json.loads(body)
    assert login["must_change_password"] is True
    token = login["access_token"]
    assert call(base, "/studio/entre-nos-dois/overview", token=token)[0] == 403

    code, body = call(base, "/staging/change-password", method="POST", token=token,
                      data=json.dumps({"current_password": initial_password, "new_password": next_password}).encode(),
                      content_type="application/json")
    assert code == 200 and json.loads(body)["changed"] is True

    code, body = call(base, "/studio/entre-nos-dois/overview", token=token)
    assert code == 200
    overview = json.loads(body)
    assert overview["auto_publish"] is False
    assert len(overview["dialogue"]) == 4

    code, body = call(base, "/studio/entre-nos-dois/references/Helena", method="PUT",
                      token=token, data=pixel_png(), content_type="application/octet-stream")
    assert code == 200 and json.loads(body)["private"] is True
    code, body = call(base, "/studio/entre-nos-dois/references/Helena/image", token=token)
    assert code == 200 and body.startswith(b"\x89PNG")
    code, body = call(base, "/studio/entre-nos-dois/readiness", token=token)
    assert code == 200
    steps = {x["key"]: x["status"] for x in json.loads(body)["steps"]}
    assert steps["private_storage"] == "configured"
    assert steps["helena_image"] == "configured"
    assert call(base, "/studio/entre-nos-dois/references/Helena/image")[0] in (401, 403)

    old_login = parse.urlencode({"username": email, "password": initial_password}).encode()
    new_login = parse.urlencode({"username": email, "password": next_password}).encode()
    assert call(base, "/token", method="POST", data=old_login,
                content_type="application/x-www-form-urlencoded")[0] == 401
    assert call(base, "/token", method="POST", data=new_login,
                content_type="application/x-www-form-urlencoded")[0] == 200

    return {"staging_e2e": "passed", "checks": 10, "billable_calls": 0, "published": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--email", required=True)
    parser.add_argument("--initial-password", required=True)
    parser.add_argument("--new-password", required=True)
    args = parser.parse_args()
    print(json.dumps(exercise(args.base_url, email=args.email,
                              initial_password=args.initial_password,
                              next_password=args.new_password)))
