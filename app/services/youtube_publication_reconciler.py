"""Publicação YouTube desacoplada da produção de vídeo.

A renderização termina independentemente do OAuth. Este serviço tenta publicar
somente artefatos já prontos e autorizados (`auto_publish=True`). Se o YouTube
estiver desconectado, nada é regenerado nem marcado como falha: o item continua
pronto e pendente para uma tentativa futura.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import UnifiedVideo, UnifiedVideoStatus
from app.services.unified_video_pipeline import unified_video_pipeline
from app.services.youtube_service import YouTubeService


def _json_object(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _extract_youtube_id(raw: Any) -> Optional[str]:
    data = raw if isinstance(raw, dict) else {}
    candidates = [
        data.get("youtube_video_id"),
        data.get("video_id"),
        data.get("videoId"),
        data.get("id"),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    nested = data.get("snippet") if isinstance(data.get("snippet"), dict) else {}
    text = str(nested.get("id") or "").strip()
    return text or None


def _metadata_for_video(row: UnifiedVideo) -> Dict[str, Any]:
    result = _json_object(getattr(row, "result_json", None))
    script = _json_object(getattr(row, "script_json", None))
    nested_script = result.get("script") if isinstance(result.get("script"), dict) else {}

    title = str(
        result.get("title")
        or result.get("youtube_title")
        or nested_script.get("title")
        or script.get("title")
        or getattr(row, "topic", None)
        or "Vídeo Codexia"
    ).strip()[:100] or "Vídeo Codexia"
    description = str(
        result.get("description")
        or nested_script.get("description")
        or script.get("description")
        or ""
    ).strip()[:5000]
    tags_value = result.get("tags")
    if not isinstance(tags_value, list):
        tags_value = nested_script.get("tags") if isinstance(nested_script.get("tags"), list) else script.get("tags")
    tags = [str(item).strip() for item in (tags_value or []) if str(item).strip()][:50]
    return {"title": title, "description": description, "tags": tags}


def _publishable_status(row: UnifiedVideo) -> bool:
    status = str(getattr(row, "status", "") or "").strip().lower()
    if status == UnifiedVideoStatus.APPROVED:
        return True
    # Um fluxo sem revisão obrigatória pode chegar pronto ainda como awaiting_review.
    if status == UnifiedVideoStatus.AWAITING_REVIEW and not bool(getattr(row, "review_required", True)):
        return True
    return False


def _video_path_ready(row: UnifiedVideo) -> bool:
    path = str(getattr(row, "video_path", None) or "").strip()
    return bool(path and os.path.isfile(path))


def reconcile_pending_youtube_publications(
    db: Session,
    *,
    user_id: Optional[int] = None,
    limit: int = 3,
    service: Optional[YouTubeService] = None,
) -> Dict[str, Any]:
    """Tenta publicar vídeos prontos que o usuário marcou para autopublicação.

    Retorna diagnóstico seguro. Desconexão/OAuth inválido nunca altera o estado
    de produção nem dispara nova geração.
    """
    safe_limit = max(1, min(20, int(limit or 3)))
    query = db.query(UnifiedVideo).filter(
        UnifiedVideo.auto_publish.is_(True),
        UnifiedVideo.youtube_video_id.is_(None),
        UnifiedVideo.video_path.isnot(None),
        UnifiedVideo.status.in_([
            UnifiedVideoStatus.APPROVED,
            UnifiedVideoStatus.AWAITING_REVIEW,
        ]),
    )
    if user_id is not None:
        query = query.filter(UnifiedVideo.user_id == int(user_id))
    rows = query.order_by(UnifiedVideo.updated_at.asc(), UnifiedVideo.id.asc()).limit(safe_limit * 4).all()
    eligible = [row for row in rows if _publishable_status(row) and _video_path_ready(row)][:safe_limit]

    yt = service or YouTubeService()
    if not getattr(yt, "service", None):
        return {
            "connected": False,
            "attempted": 0,
            "published": 0,
            "pending": len(eligible),
            "message": "YouTube desconectado; vídeos prontos foram preservados e continuam aguardando publicação.",
        }

    pipeline = unified_video_pipeline()
    results: List[Dict[str, Any]] = []
    published = 0
    for row in eligible:
        metadata = _metadata_for_video(row)

        def _upload(video_path: str, upload_metadata: Dict[str, Any], _yt: YouTubeService = yt) -> Dict[str, Any]:
            meta = dict(upload_metadata or {})
            raw = _yt.upload_video(
                str(video_path),
                title=str(meta.get("title") or "Vídeo Codexia")[:100],
                description=str(meta.get("description") or "")[:5000],
                tags=[str(item) for item in (meta.get("tags") or []) if str(item).strip()],
            )
            if isinstance(raw, dict):
                video_id = _extract_youtube_id(raw)
                return {
                    **raw,
                    "youtube_video_id": video_id,
                    "youtube_url": (f"https://www.youtube.com/watch?v={video_id}" if video_id else None),
                }
            return {"youtube_video_id": None, "raw": str(raw or "")[:1000]}

        outcome = pipeline.publish_if_ready(
            db,
            idempotency_key_or_task_id=str(row.task_id or row.idempotency_key),
            upload_callable=_upload,
            upload_metadata=metadata,
            visibility_override=str(getattr(row, "visibility", None) or "unlisted"),
        )
        ok = bool(isinstance(outcome, dict) and outcome.get("ok"))
        if ok:
            published += 1
        results.append({
            "unified_video_id": int(row.id),
            "ok": ok,
            "code": (outcome.get("code") if isinstance(outcome, dict) else None),
            "youtube_video_id": (outcome.get("youtube_video_id") if isinstance(outcome, dict) else None),
        })

    return {
        "connected": True,
        "attempted": len(eligible),
        "published": published,
        "pending": max(0, len(eligible) - published),
        "results": results,
    }
