"""Consulta e validação do resultado da primeira cena Sync-3.

Não solicita novas gerações nem considera a cena aprovada. URL final do vídeo
deve ser armazenada como dado sensível e copiada para storage privado após a
revisão. A consulta não faz download nem publica conteúdo.
"""
from __future__ import annotations

from typing import Callable

from app.services.entre_nos_dois_fal_sync3 import _safe_https_asset_url
from app.services.entre_nos_dois_sync3_queue import (
    QUEUE_STATUS_BASE,
    Sync3QueueError,
    _json_request,
    _request_id,
    check_sync3_status,
)


def fetch_sync3_result(
    *,
    request_id: str,
    api_key: str,
    transport: Callable[[str, str, str, dict | None], dict] = _json_request,
) -> dict:
    """Busca o resultado somente depois de status COMPLETED sem erro."""
    rid = _request_id(request_id)
    status_url = f"{QUEUE_STATUS_BASE}/requests/{rid}/status"
    current = transport("GET", status_url, api_key, None)
    if not isinstance(current, dict):
        raise Sync3QueueError("Estado da fila inválido.")
    if current.get("status") != "COMPLETED":
        raise Sync3QueueError("Resultado não está concluído na fila.")
    if current.get("error") or current.get("error_type"):
        raise Sync3QueueError("Falha do provedor; é necessária revisão humana.")
    result_url = f"{QUEUE_STATUS_BASE}/requests/{rid}"
    result = transport("GET", result_url, api_key, None)
    if not isinstance(result, dict):
        raise Sync3QueueError("Resposta final inválida.")
    video = result.get("video")
    if not isinstance(video, dict) or not _safe_https_asset_url(video.get("url")):
        raise Sync3QueueError("Vídeo final sem URL HTTPS validável.")
    if video.get("content_type") not in (None, "video/mp4"):
        raise Sync3QueueError("Formato de vídeo inesperado.")
    return {
        "request_id": rid,
        "status": "awaiting_manual_video_review",
        "video_url": video["url"],
        "video_content_type": "video/mp4",
        "requires_download_to_private_storage": True,
        "requires_lip_sync_quality_review": True,
        "requires_identity_consistency_review": True,
        "approved_for_publication": False,
        "auto_publish": False,
        "additional_billable_requests": 0,
    }
