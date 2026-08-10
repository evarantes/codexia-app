import os
import sys
from datetime import datetime

import requests


def _token(base_url: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{base_url}/token",
        data={"username": username, "password": password},
        timeout=20,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("access_token") or ""
    if not token:
        raise RuntimeError("token_missing")
    return token


def main() -> int:
    base_url = (os.getenv("CODEXIA_BASE_URL") or "http://127.0.0.1:8011").rstrip("/")
    username = os.getenv("CODEXIA_ADMIN_EMAIL") or "admin@codexia.dev"
    password = os.getenv("CODEXIA_ADMIN_PASSWORD") or "admin123"

    token = _token(base_url, username, password)
    headers = {"Authorization": f"Bearer {token}"}

    r = requests.get(f"{base_url}/bible-video-factory/bootstrap", headers=headers, timeout=30)
    sys.stdout.write(f"BOOTSTRAP {r.status_code}\n")
    r.raise_for_status()
    boot = r.json() or {}
    config = boot.get("config") or {}
    sys.stdout.write(f"BOOT_TEXT_PROVIDER {config.get('text_provider')}\n")
    sys.stdout.write(f"BOOT_IMAGE_PROVIDER {config.get('image_provider')}\n")

    theme = os.getenv("CODEXIA_BIBLE_THEME") or f"Tema de homologação {datetime.utcnow().isoformat()}"
    create = requests.post(
        f"{base_url}/bible-video-factory/series",
        headers=headers,
        json={"name": theme, "planned_episodes": 1},
        timeout=30,
    )
    sys.stdout.write(f"CREATE_SERIES {create.status_code}\n")
    create.raise_for_status()
    series = create.json() or {}
    series_id = int(series.get("id") or 0)
    if not series_id:
        raise RuntimeError("series_id_missing")

    split = requests.post(
        f"{base_url}/bible-video-factory/series/{series_id}/split-episodes",
        headers=headers,
        json={"replace_existing": True},
        timeout=30,
    )
    sys.stdout.write(f"SPLIT_EPISODES {split.status_code}\n")
    split.raise_for_status()
    episodes = split.json() or []
    if isinstance(episodes, list) and episodes:
        episode_id = int((episodes[0] or {}).get("id") or 0)
    else:
        episode_id = 0
    if not episode_id:
        raise RuntimeError("episode_id_missing")

    script_resp = requests.post(
        f"{base_url}/bible-video-factory/episodes/{episode_id}/generate-script",
        headers=headers,
        json={},
        timeout=60,
    )
    sys.stdout.write(f"GENERATE_SCRIPT {script_resp.status_code}\n")
    script_resp.raise_for_status()
    script = script_resp.json() or {}
    script_id = int(script.get("id") or 0)
    if not script_id:
        raise RuntimeError("script_id_missing")

    scenes_resp = requests.post(
        f"{base_url}/bible-video-factory/scripts/{script_id}/generate-scenes",
        headers=headers,
        json={},
        timeout=60,
    )
    sys.stdout.write(f"GENERATE_SCENES {scenes_resp.status_code}\n")
    scenes_resp.raise_for_status()
    scenes = scenes_resp.json() or []
    sys.stdout.write(f"SCENES_COUNT {len(scenes) if isinstance(scenes, list) else 0}\n")

    validate_resp = requests.post(
        f"{base_url}/bible-video-factory/scripts/{script_id}/validate",
        headers=headers,
        json={},
        timeout=60,
    )
    sys.stdout.write(f"VALIDATE_SCRIPT {validate_resp.status_code}\n")
    validate_resp.raise_for_status()
    validated = validate_resp.json() or {}
    blueprint = validated.get("production_blueprint") or {}
    sys.stdout.write(f"PIPELINE_STATUS {blueprint.get('pipeline_status')}\n")
    sys.stdout.write("PROVIDER_SELECTION VIA_BOOTSTRAP YES\n")

    sys.stdout.write("STOP_BEFORE_IMAGE_CALLS YES\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
