"""Authenticated internal capability check. Never returns secrets or spends credits."""
import hmac
import os
from fastapi import APIRouter, Header, HTTPException
from app.services.suno_service import get_suno_api_key

router = APIRouter(prefix="/api/internal/codexia", tags=["internal-codexia"])

@router.get("/music-capability")
def music_capability(authorization: str | None = Header(default=None)) -> dict:
    expected = os.getenv("CODEXIA_INTERNAL_SERVICE_TOKEN", "").strip()
    if len(expected) < 32:
        raise HTTPException(503, "Internal service authentication is not configured")
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "Unauthorized")
    return {"suno_available": bool(get_suno_api_key()), "provider": "suno"}
