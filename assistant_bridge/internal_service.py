"""Private server-to-server capability handshake for Codexia installations.

This endpoint never returns API keys or invokes paid providers.
"""
import hmac
import os
from fastapi import APIRouter, Header, HTTPException
from app.services.suno_service import get_suno_api_key

router = APIRouter(prefix="/api/internal/codexia", tags=["internal-codexia"])

@router.get("/music-capability")
def music_capability(authorization: str | None = Header(default=None)) -> dict:
    secret = os.getenv("CODEXIA_INTERNAL_SERVICE_TOKEN", "").strip()
    if len(secret) < 32:
        raise HTTPException(503, "Internal service authentication is not configured")
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(401, "Unauthorized")
    # Only administrator-shared settings are eligible for cross-installation use.
    return {"suno_available": bool(get_suno_api_key()), "provider": "suno"}
