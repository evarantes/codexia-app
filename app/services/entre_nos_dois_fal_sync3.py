"""Contrato de integração fal.ai Sync-3 (imagem + áudio), sem requisições pagas.

URLs temporárias devem ser emitidas pelo storage do projeto com permissão de
leitura estritamente limitada. Este módulo apenas prepara o payload para um
worker que terá persistência, idempotência e confirmação de custos.
"""
from __future__ import annotations

import ipaddress
import math
from urllib.parse import urlsplit

from app.services.entre_nos_dois_lipsync_gate import prepare_lip_sync_jobs, LipSyncGateError

FAL_MODEL = "fal-ai/sync-lipsync/v3/image-to-video"
USD_PER_OUTPUT_SECOND_REFERENCE = 0.1333


def _safe_https_asset_url(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            return False
        host = parts.hostname.lower()
        if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
            return False
        try:
            addr = ipaddress.ip_address(host)
            return addr.is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def prepare_fal_sync3_preview(
    *,
    timeline: dict,
    reference_keys: dict[str, str],
    asset_urls: dict[int, dict[str, str]],
    approved_max_usd: float | None = None,
    price_usd_per_second: float = USD_PER_OUTPUT_SECOND_REFERENCE,
) -> dict:
    """Só prepara o primeiro trecho; deliberadamente não dispara geração.

    preço é referência de planejamento, não uma cotação contratual da conta.
    Os links assinados devem ser curtos e emitidos apenas após autorização
    de envio dos arquivos ao provedor externo.
    """
    plan = prepare_lip_sync_jobs(timeline=timeline, reference_keys=reference_keys)
    visual = plan["estimated_scene_count"]
    if visual != 4:
        raise LipSyncGateError("Piloto precisa conter quatro cenas.")
    if not math.isfinite(price_usd_per_second) or price_usd_per_second <= 0:
        raise LipSyncGateError("Preço unitário inválido.")
    first = timeline["segments"][0]
    seconds = float(first["measured_duration_seconds"])
    estimated = round(seconds * price_usd_per_second, 4)
    response = {
        "provider": "fal.ai",
        "model": FAL_MODEL,
        "scene_index": 1,
        "character": first["speaker"],
        "duration_seconds": seconds,
        "estimated_usd": estimated,
        "cost_is_estimate": True,
        "billable_calls_performed": False,
        "auto_publish": False,
        "status": "awaiting_asset_and_cost_approval",
    }
    if approved_max_usd is None:
        return response
    if not math.isfinite(approved_max_usd) or approved_max_usd <= 0 or estimated > approved_max_usd:
        raise LipSyncGateError("Custo estimado superior ao teto autorizado.")
    assets = asset_urls.get(1) if isinstance(asset_urls, dict) else None
    if not isinstance(assets, dict):
        raise LipSyncGateError("URLs assinadas ainda não preparadas para a primeira cena.")
    image_url = assets.get("image_url")
    audio_url = assets.get("audio_url")
    if not _safe_https_asset_url(image_url) or not _safe_https_asset_url(audio_url):
        raise LipSyncGateError("Links de mídia precisam ser HTTPS públicos, sem redes privadas.")
    response["request_input"] = {
        "image_url": image_url,
        "audio_url": audio_url,
        "enable_safety_checker": True,
    }
    response["approved_max_usd"] = approved_max_usd
    response["status"] = "ready_for_explicit_submission"
    return response
