"""
Router unificado para publicação em redes sociais.
Endpoints para Facebook, Instagram e TikTok.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.database import get_db
import os

router = APIRouter(prefix="/social", tags=["Social Media"])


class PostTextRequest(BaseModel):
    content: str
    link: Optional[str] = None
    book_id: Optional[int] = None


class PostVideoRequest(BaseModel):
    video_url: Optional[str] = None
    video_path: Optional[str] = None
    title: Optional[str] = ""
    description: Optional[str] = ""
    caption: Optional[str] = ""
    privacy_level: Optional[str] = "SELF_ONLY"


class MultiPostRequest(BaseModel):
    """Publicar em múltiplas plataformas de uma vez."""
    video_url: Optional[str] = None
    video_path: Optional[str] = None
    title: Optional[str] = ""
    description: Optional[str] = ""
    caption: Optional[str] = ""
    platforms: list[str] = []


# ─── Status ──────────────────────────────────────────────
@router.get("/status")
def social_status(db: Session = Depends(get_db)):
    """Retorna o status de configuração de cada rede social."""
    from app.models import Settings
    settings = db.query(Settings).first()

    def _has(val):
        return bool(val and str(val).strip() and str(val).strip() not in ("seu_token_de_acesso_da_pagina", "YOUR_TOKEN"))

    fb_ok = _has(getattr(settings, "facebook_page_id", None)) and _has(getattr(settings, "facebook_access_token", None))
    ig_ok = _has(getattr(settings, "instagram_user_id", None)) and _has(getattr(settings, "instagram_access_token", None) or getattr(settings, "facebook_access_token", None))
    tt_ok = _has(getattr(settings, "tiktok_access_token", None))
    yt_ok = _has(getattr(settings, "youtube_client_id", None)) and _has(getattr(settings, "youtube_client_secret", None))

    return {
        "facebook": {"configured": fb_ok, "features": ["feed", "video", "reels"]},
        "instagram": {"configured": ig_ok, "features": ["reels", "image"]},
        "tiktok": {"configured": tt_ok, "features": ["video"]},
        "youtube": {"configured": yt_ok, "features": ["video", "shorts"]},
    }


# ─── Facebook ────────────────────────────────────────────
@router.post("/facebook/post")
def facebook_post_text(req: PostTextRequest):
    """Publica texto (e link opcional) na fanpage do Facebook."""
    from app.services.facebook_api import FacebookService
    fb = FacebookService()
    result = fb.post_to_feed(req.content, link=req.link)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/facebook/video")
def facebook_post_video(req: PostVideoRequest):
    """Publica um vídeo no feed da fanpage do Facebook."""
    from app.services.facebook_api import FacebookService
    fb = FacebookService()
    path = _resolve_video_path(req.video_path, req.video_url)
    result = fb.post_video(path, title=req.title or "", description=req.description or "")
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/facebook/reels")
def facebook_post_reels(req: PostVideoRequest):
    """Publica um Reel na fanpage do Facebook."""
    from app.services.facebook_api import FacebookService
    fb = FacebookService()
    path = _resolve_video_path(req.video_path, req.video_url)
    result = fb.post_reels(path, description=req.description or req.caption or "")
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ─── Instagram ───────────────────────────────────────────
@router.post("/instagram/reels")
def instagram_post_reels(req: PostVideoRequest):
    """Publica um Reel no Instagram. Requer video_url pública."""
    from app.services.instagram_api import InstagramService
    ig = InstagramService()
    video_url = req.video_url
    if not video_url:
        raise HTTPException(
            status_code=400,
            detail="Instagram requer video_url pública. Não é possível enviar arquivo local diretamente."
        )
    result = ig.publish_reels(video_url, caption=req.caption or req.description or "")
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/instagram/image")
def instagram_post_image(req: PostTextRequest):
    """Publica uma imagem no feed do Instagram. Requer URL pública no campo link."""
    from app.services.instagram_api import InstagramService
    ig = InstagramService()
    if not req.link:
        raise HTTPException(status_code=400, detail="Informe a URL pública da imagem no campo 'link'.")
    result = ig.publish_image(req.link, caption=req.content or "")
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ─── TikTok ──────────────────────────────────────────────
@router.post("/tiktok/video")
def tiktok_post_video(req: PostVideoRequest):
    """Publica um vídeo no TikTok via upload direto."""
    from app.services.tiktok_api import TikTokService
    tt = TikTokService()
    path = _resolve_video_path(req.video_path, req.video_url)
    if path and os.path.exists(path):
        result = tt.publish_video(path, title=req.title or "", privacy_level=req.privacy_level or "SELF_ONLY")
    elif req.video_url:
        result = tt.publish_video_by_url(req.video_url, title=req.title or "", privacy_level=req.privacy_level or "SELF_ONLY")
    else:
        raise HTTPException(status_code=400, detail="Informe video_path ou video_url.")
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ─── Multi-plataforma ────────────────────────────────────
@router.post("/publish")
def multi_publish(req: MultiPostRequest):
    """
    Publica em múltiplas redes de uma vez.
    platforms: lista com combinação de 'facebook', 'instagram', 'tiktok', 'youtube'.
    """
    results = {}
    video_path = _resolve_video_path(req.video_path, req.video_url)

    for platform in req.platforms:
        platform = platform.lower().strip()
        try:
            if platform == "facebook":
                from app.services.facebook_api import FacebookService
                fb = FacebookService()
                if video_path and os.path.exists(video_path):
                    results["facebook"] = fb.post_reels(video_path, description=req.description or req.caption or "")
                else:
                    results["facebook"] = fb.post_to_feed(req.description or req.caption or "", link=req.video_url)

            elif platform == "instagram":
                from app.services.instagram_api import InstagramService
                ig = InstagramService()
                if req.video_url:
                    results["instagram"] = ig.publish_reels(req.video_url, caption=req.caption or req.description or "")
                else:
                    results["instagram"] = {"error": "Instagram requer video_url pública."}

            elif platform == "tiktok":
                from app.services.tiktok_api import TikTokService
                tt = TikTokService()
                if video_path and os.path.exists(video_path):
                    results["tiktok"] = tt.publish_video(video_path, title=req.title or "")
                elif req.video_url:
                    results["tiktok"] = tt.publish_video_by_url(req.video_url, title=req.title or "")
                else:
                    results["tiktok"] = {"error": "Informe video_path ou video_url."}

            elif platform == "youtube":
                results["youtube"] = {"status": "use_youtube_router", "detail": "Use o endpoint /youtube/upload para publicar no YouTube."}

            else:
                results[platform] = {"error": f"Plataforma '{platform}' não suportada."}
        except Exception as e:
            results[platform] = {"error": str(e)}

    return {"results": results}


# ─── Helpers ─────────────────────────────────────────────
def _resolve_video_path(video_path: str = None, video_url: str = None) -> str:
    """Resolve path do vídeo a partir de path local ou URL relativa."""
    if video_path:
        p = video_path.strip()
        if p.startswith("/static/"):
            candidate = os.path.join("app", p.lstrip("/"))
            if os.path.exists(candidate):
                return candidate
        if p.startswith("/media/videos/"):
            from app.config import VIDEO_OUTPUT_DIR
            name = os.path.basename(p)
            candidate = os.path.join(VIDEO_OUTPUT_DIR, name)
            if os.path.exists(candidate):
                return candidate
        if os.path.exists(p):
            return p
    if video_url and not (video_url.startswith("http://") or video_url.startswith("https://")):
        return _resolve_video_path(video_path=video_url)
    return video_path or ""
