"""Gate de sincronização labial: valida insumos e orçamento antes de usar IA.

Este módulo NÃO gera vídeo. Um provedor de lip-sync deve ser ligado apenas
depois de confirmar endpoint, compatibilidade e preço; Runway Act Two exige
vídeo de atuação, não é um substituto direto de imagem + MP3.
"""
from __future__ import annotations

import math
from pathlib import Path

from app.services.entre_nos_dois import HOSTS
from app.services.entre_nos_dois_visual_plan import build_visual_pilot_plan


class LipSyncGateError(ValueError):
    pass


def prepare_lip_sync_jobs(
    *,
    timeline: dict,
    reference_keys: dict[str, str],
    approved_provider: str | None = None,
    approved_max_credits: float | None = None,
    provider_estimate_credits: float | None = None,
) -> dict:
    """Bloqueia geração paga quando fornecedor/custo não estiver confirmado.

    Retorna planos de trabalho para um adaptador futuro, jamais faz POST.
    Caminhos locais de áudio permanecem no worker; chaves privadas de
    imagens devem ser resolvidas para URLs temporárias pelo storage.
    """
    visual = build_visual_pilot_plan(timeline=timeline, reference_keys=reference_keys)
    if not approved_provider or approved_max_credits is None or provider_estimate_credits is None:
        return {
            "status": "provider_and_cost_review_required",
            "jobs_ready": False,
            "estimated_scene_count": len(visual["scenes"]),
            "auto_publish": False,
            "billable_calls_performed": False,
        }
    if approved_provider not in {"lipsync_image_audio"}:
        raise LipSyncGateError("Provedor sem contrato de imagem + áudio aprovado.")
    if not math.isfinite(approved_max_credits) or approved_max_credits <= 0:
        raise LipSyncGateError("Limite de créditos inválido.")
    if not math.isfinite(provider_estimate_credits) or provider_estimate_credits < 0:
        raise LipSyncGateError("Estimativa do provedor inválida.")
    if provider_estimate_credits > approved_max_credits:
        raise LipSyncGateError("Estimativa excede o orçamento aprovado.")
    identities = {host.name: host.provider_voice_id for host in HOSTS}
    jobs = []
    for scene in visual["scenes"]:
        if scene["voice_id"] != identities[scene["active_speaker"]]:
            raise LipSyncGateError("Identidade vocal incorreta.")
        audio = Path(scene["audio_path"])
        if not audio.is_file() or audio.suffix.lower() != ".mp3":
            raise LipSyncGateError("Arquivo MP3 necessário para sincronização.")
        jobs.append({
            "scene_index": scene["scene_index"],
            "character": scene["active_speaker"],
            "reference_storage_key": scene["private_reference_key"],
            "audio_local_path": str(audio.resolve()),
            "duration_seconds": round(scene["end_seconds"] - scene["start_seconds"], 3),
            "input_type": "single_character_image_and_audio",
            "provider_job_id": None,
            "status": "awaiting_provider_adapter",
        })
    return {
        "program": visual["program"],
        "project_id": visual["project_id"],
        "approved_provider_contract": approved_provider,
        "approved_max_credits": approved_max_credits,
        "provider_estimate_credits": provider_estimate_credits,
        "jobs": jobs,
        "jobs_ready": True,
        "status": "awaiting_provider_adapter",
        "billable_calls_performed": False,
        "auto_publish": False,
    }
