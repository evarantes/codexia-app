#!/usr/bin/env python3
"""Real HTTP staging acceptance: login, password rotation and Drive fail-closed behavior.

Uses disposable test credentials. Media uploads run only when a Drive account is
configured; otherwise staging verifies uploads are rejected safely. No paid calls.
Never run against production, real accounts or real portraits.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
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
    assert call(base, "/studio/entre-nos-dois/references/Helena/image")[0] in (401, 403)
    if steps["private_storage"] != "configured":
        # CI has no real Drive account. Prove that storage is required and the
        # admin upload fails closed instead of retaining media on the app disk.
        assert steps["private_storage"] == "missing"
        code, _ = call(base, "/studio/entre-nos-dois/references/Helena", method="PUT",
                       token=token, data=pixel_png(), content_type="application/octet-stream")
        assert code in (400, 503)
        code, body = call(base, "/studio/entre-nos-dois/scene/readiness", token=token)
        assert code == 200 and json.loads(body)["inputs_ready"] is False
        old_login = parse.urlencode({"username": email, "password": initial_password}).encode()
        new_login = parse.urlencode({"username": email, "password": next_password}).encode()
        assert call(base, "/token", method="POST", data=old_login,
                    content_type="application/x-www-form-urlencoded")[0] == 401
        assert call(base, "/token", method="POST", data=new_login,
                    content_type="application/x-www-form-urlencoded")[0] == 200
        return {"staging_e2e": "passed", "media_storage": "blocked_without_drive", "billable_calls": 0, "published": False}
    assert steps["helena_image"] == "configured"

    code, body = call(base, "/studio/entre-nos-dois/voice-cost-worksheet", token=token)
    assert code == 200
    budget = json.loads(body)
    assert budget["generation_enabled"] is False and budget["billable_calls_performed"] is False
    assert budget["total_characters"] > 0 and len(budget["manifest_sha256"]) == 64
    assert call(base, "/studio/entre-nos-dois/voice-cost-worksheet")[0] in (401, 403)
    code, body = call(base, "/studio/entre-nos-dois/voice-cost-worksheet?usd_per_1000_characters=0.25", token=token)
    assert code == 200 and json.loads(body)["estimate_only"] is True

    # Even an authenticated admin cannot spend without explicit environment switch.
    code, body = call(
        base, "/studio/entre-nos-dois/audio/generate/1", token=token, method="POST",
        data=json.dumps({
            "quote_digest": budget["manifest_sha256"],
            "usd_per_1000_characters": "0.25",
            "approved_max_usd": "0.10",
            "confirmation": "AUTORIZO FALA 1",
            "rate_checked_in_provider_account": True,
        }).encode(), content_type="application/json",
    )
    assert code == 403, "Staging must default to no paid TTS even with an approved payload."

    code, body = call(base, "/studio/entre-nos-dois/scene/readiness", token=token)
    assert code == 200
    scene_before = json.loads(body)
    assert scene_before["inputs_ready"] is False
    assert scene_before["billable_calls_performed"] is False
    assert call(base, "/studio/entre-nos-dois/scene/readiness")[0] in (401, 403)

    # Real 1-second disposable MP3 with no speech or copyrighted content.
    mp3 = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-ac", "1", "-ar", "16000",
         "-b:a", "16k", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True, timeout=15,
    ).stdout
    assert mp3.startswith(b"ID3") and len(mp3) > 500
    for index in (1, 2, 3, 4):
        code, body = call(base, f"/studio/entre-nos-dois/audio/{index}", method="PUT", token=token,
                          data=mp3, content_type="audio/mpeg")
        assert code == 200 and json.loads(body)["private"] is True
        code, body = call(base, f"/studio/entre-nos-dois/audio/{index}/file", token=token)
        assert code == 200 and body.startswith(b"ID3")
        assert call(base, f"/studio/entre-nos-dois/audio/{index}/file")[0] in (401, 403)
    code, body = call(base, "/studio/entre-nos-dois/audio", token=token)
    assert code == 200 and json.loads(body)["all_uploaded"] is True
    code, body = call(base, "/studio/entre-nos-dois/audio/timeline", token=token)
    assert code == 200
    timeline = json.loads(body)
    assert timeline["ready"] is True and len(timeline["segments"]) == 4
    assert 4.0 <= timeline["total_seconds"] <= 6.0
    assert timeline["is_rendered_video"] is False and timeline["auto_publish"] is False
    assert call(base, "/studio/entre-nos-dois/audio/timeline")[0] in (401, 403)

    code, body = call(base, "/studio/entre-nos-dois/scene/readiness?usd_per_second=0.20", token=token)
    assert code == 200
    scene_after = json.loads(body)
    assert scene_after["inputs_ready"] is False  # Gabriel reference not uploaded.
    assert "reference_gabriel" in scene_after["missing"]
    assert scene_after["private_media_export_authorized"] is False
    assert scene_after["signed_asset_urls_created"] is False
    assert scene_after["billable_calls_performed"] is False

    # Complete visual inputs: a full four-scene plan is now ready for
    # *human provider review* but still cannot export assets or spend money.
    code, body = call(base, "/studio/entre-nos-dois/references/Gabriel", method="PUT",
                      token=token, data=pixel_png(), content_type="application/octet-stream")
    assert code == 200 and json.loads(body)["private"] is True
    code, body = call(base, "/studio/entre-nos-dois/scene/readiness?usd_per_second=0.20", token=token)
    assert code == 200
    scene_complete = json.loads(body)
    assert scene_complete["inputs_ready"] is True
    assert len(scene_complete["scenes"]) == 4
    assert scene_complete["missing"] == []
    assert scene_complete["estimated_total_usd"] is not None
    assert scene_complete["generation_enabled"] is False
    assert scene_complete["private_media_export_authorized"] is False
    assert scene_complete["signed_asset_urls_created"] is False
    assert scene_complete["rendered_video_ready"] is False
    assert scene_complete["auto_publish"] is False

    old_login = parse.urlencode({"username": email, "password": initial_password}).encode()
    new_login = parse.urlencode({"username": email, "password": next_password}).encode()
    assert call(base, "/token", method="POST", data=old_login,
                content_type="application/x-www-form-urlencoded")[0] == 401
    assert call(base, "/token", method="POST", data=new_login,
                content_type="application/x-www-form-urlencoded")[0] == 200

    return {"staging_e2e": "passed", "checks": 30, "billable_calls": 0, "published": False}


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
