"""Pré-verificação sem geração paga das vozes aprovadas do Entre Nós Dois.

O catálogo de um intermediário não garante acesso aos IDs pela API direta.
A verificação consulta apenas metadados por GET, sem gerar áudio.
"""
from __future__ import annotations

import json
from urllib import error, request
from typing import Callable, Optional

from app.services.entre_nos_dois import HOSTS

API_BASE = "https://api.elevenlabs.io/v1"


class VoiceAccessError(RuntimeError):
    pass


def _get_voice_metadata(voice_id: str, api_key: str) -> dict:
    req = request.Request(
        f"{API_BASE}/voices/{voice_id}",
        method="GET",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise VoiceAccessError(
            f"Consulta da voz não autorizada/disponível (HTTP {exc.code}); "
            "nenhum áudio foi solicitado."
        ) from None
    except (error.URLError, TimeoutError, ValueError):
        raise VoiceAccessError(
            "Não foi possível verificar a disponibilidade da voz; "
            "nenhum áudio foi solicitado."
        ) from None


def check_approved_voices(
    *,
    api_key: str,
    fetch_metadata: Optional[Callable[[str, str], dict]] = None,
) -> dict:
    """Confirma acesso aos dois IDs de voz pela API que fará a síntese.

    Não aceita apenas um nome semelhante, não altera as vozes aprovadas
    e não realiza chamadas POST de TTS.
    """
    if not api_key or not api_key.strip():
        raise ValueError("A chave de API deve estar configurada em ambiente seguro.")
    fetch_metadata = fetch_metadata or _get_voice_metadata
    checked = []
    for host in HOSTS:
        if not host.provider_voice_id:
            raise VoiceAccessError(f"ID da voz de {host.name} não configurado.")
        try:
            info = fetch_metadata(host.provider_voice_id, api_key)
        except VoiceAccessError:
            raise
        except Exception:
            raise VoiceAccessError(
                f"Não foi possível verificar a voz de {host.name}; "
                "nenhum áudio foi solicitado."
            ) from None
        if not isinstance(info, dict) or info.get("voice_id") != host.provider_voice_id:
            raise VoiceAccessError(
                f"ID da voz de {host.name} não confirmado pela API; "
                "nenhum áudio foi solicitado."
            )
        checked.append({
            "speaker": host.name,
            "voice_id": host.provider_voice_id,
            "provider_name": str(info.get("name") or "")[:120],
            "accessible": True,
        })
    return {
        "program": "entre-nos-dois",
        "checked": checked,
        "all_accessible": True,
        "billable_generation_performed": False,
        "next_step": "cost_approval_then_audio_test",
    }
