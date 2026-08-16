from __future__ import annotations

import os


def _enabled(name: str, default: str = "false") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def apply_visual_quality_observe_rollout() -> dict:
    """Rollout progressivo do Fiscal Visual no worker CX33.

    Segurança do estágio atual:
    - crítico multimodal ligado por padrão;
    - rejeição estrita ligada por padrão SOMENTE para defeitos críticos do guard;
    - no máximo 1 regeneração da imagem reprovada;
    - fail-closed permanece desligado: se crítico/retry falhar, o vídeo continua;
    - qualquer variável explícita do ambiente prevalece para rollback imediato.
    """
    defaults = {
        "ENABLE_VISUAL_CRITIC_AI": "true",
        "ENABLE_STRICT_VISUAL_REJECT": "true",
        "VISUAL_QA_MAX_RETRIES": "1",
        "VISUAL_QA_FAIL_CLOSED": "false",
    }
    for name, value in defaults.items():
        if name not in os.environ:
            os.environ[name] = value

    try:
        retries = max(0, min(1, int(str(os.getenv("VISUAL_QA_MAX_RETRIES") or "1").strip())))
    except Exception:
        retries = 1

    return {
        "ai_critic_enabled": _enabled("ENABLE_VISUAL_CRITIC_AI"),
        "strict_visual_reject": _enabled("ENABLE_STRICT_VISUAL_REJECT"),
        "fail_closed": _enabled("VISUAL_QA_FAIL_CLOSED"),
        "max_retries": retries,
        "model": str(os.getenv("VISUAL_QA_MODEL") or "gpt-4.1-mini").strip() or "gpt-4.1-mini",
    }
