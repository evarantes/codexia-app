"""Remote Codexia capability client. No API key is ever transferred."""
import os
from urllib.parse import urlparse
import requests

def remote_music_capability() -> dict:
    base = os.getenv("CODEXIA_MAIN_INTERNAL_URL", "").strip().rstrip("/")
    token = os.getenv("CODEXIA_INTERNAL_SERVICE_TOKEN", "").strip()
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return {"configured": False, "suno_available": False, "reason": "Main Codexia HTTPS URL not configured"}
    if len(token) < 32:
        return {"configured": False, "suno_available": False, "reason": "Internal service token not configured"}
    try:
        response = requests.get(
            base + "/api/internal/codexia/music-capability",
            headers={"Authorization": "Bearer " + token},
            timeout=8,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        return {"configured": True, "suno_available": payload.get("suno_available") is True}
    except (requests.RequestException, ValueError):
        return {"configured": True, "suno_available": False, "reason": "Main Codexia capability check failed"}
