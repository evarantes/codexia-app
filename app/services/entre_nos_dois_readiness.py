"""Readiness snapshot without live network calls or revealing credential values."""
from __future__ import annotations

from typing import Mapping


def summarize_readiness(*, elevenlabs_configured: bool, fal_configured: bool) -> dict:
    """Configuration presence is not verification of provider access."""
    steps = [
        {"key": "voice_ids", "label": "Vozes Andrey e Luciana", "status": "configured", "detail": "Identificadores registrados"},
        {"key": "elevenlabs_key", "label": "Credencial ElevenLabs", "status": "configured" if elevenlabs_configured else "missing", "detail": "Presença da chave; acesso às vozes ainda não verificado"},
        {"key": "fal_key", "label": "Credencial fal.ai", "status": "configured" if fal_configured else "missing", "detail": "Presença da chave; saldo e custo não verificados"},
        {"key": "visual_assets", "label": "Imagens oficiais privadas", "status": "not_verified", "detail": "Ainda não há confirmação dos arquivos no storage"},
        {"key": "actual_audio", "label": "Áudio real", "status": "not_generated", "detail": "Nenhuma fala gerada por este fluxo"},
        {"key": "lip_sync", "label": "Sincronização labial", "status": "not_generated", "detail": "Aguardar áudio e referências validados"},
    ]
    return {
        "steps": steps,
        "paid_execution_enabled": False,
        "auto_publish": False,
        "live_provider_access_confirmed": False,
    }
