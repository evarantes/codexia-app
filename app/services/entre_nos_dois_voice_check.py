"""Executa a checagem gratuita de acesso às vozes oficiais, sem gerar áudio.

Uso no ambiente seguro do Codexia:
  python -m app.services.entre_nos_dois_voice_check

Não imprime a chave da API, não altera Settings e não faz chamadas pagas.
"""
from __future__ import annotations

import json

from app.services.entre_nos_dois_voice_preflight import VoiceAccessError, check_approved_voices


def run_voice_check(*, provider_settings=None, checker=check_approved_voices) -> dict:
    """Lê a credencial global existente e confirma as vozes oficiais."""
    if provider_settings is None:
        from app.services.provider_config import resolve_global_provider_settings
        provider_settings = resolve_global_provider_settings()
    secret = provider_settings.get("elevenlabs_api_key") or {}
    key = secret.get("value") if isinstance(secret, dict) else None
    if not key:
        return {
            "status": "configuration_required",
            "message": "Configure ElevenLabs em Settings antes de verificar as vozes.",
            "billable_generation_performed": False,
        }
    try:
        result = checker(api_key=key)
    except (VoiceAccessError, ValueError):
        return {
            "status": "voice_access_unconfirmed",
            "message": "As vozes aprovadas não foram confirmadas na API direta. Sem cobrança ou substituição.",
            "billable_generation_performed": False,
        }
    if not result.get("all_accessible"):
        return {
            "status": "voice_access_unconfirmed",
            "message": "Uma ou mais vozes não foram confirmadas. Sem geração paga.",
            "billable_generation_performed": False,
        }
    return {
        "status": "ready_for_cost_approval",
        "voices": [
            {"speaker": item["speaker"], "provider_name": item["provider_name"], "accessible": True}
            for item in result["checked"]
        ],
        "billable_generation_performed": False,
    }


def main() -> int:
    result = run_voice_check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ready_for_cost_approval" else 1


if __name__ == "__main__":
    raise SystemExit(main())
