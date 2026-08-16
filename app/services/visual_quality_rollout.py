from __future__ import annotations

import os


def apply_visual_quality_observe_rollout() -> dict:
    """Ativa apenas a crítica visual em modo observação no worker.

    Regras de segurança:
    - respeita override explícito do ambiente;
    - não ativa rejeição estrita;
    - não ativa fail-closed;
    - não aumenta retries;
    - usa o modelo já definido pelo guard (gpt-4.1-mini por padrão).
    """
    if "ENABLE_VISUAL_CRITIC_AI" not in os.environ:
        os.environ["ENABLE_VISUAL_CRITIC_AI"] = "true"

    return {
        "ai_critic_enabled": str(os.getenv("ENABLE_VISUAL_CRITIC_AI") or "").strip().lower()
        in {"1", "true", "yes", "sim", "on", "enabled", "enable"},
        "strict_visual_reject": str(os.getenv("ENABLE_STRICT_VISUAL_REJECT") or "false").strip().lower()
        in {"1", "true", "yes", "sim", "on", "enabled", "enable"},
        "fail_closed": str(os.getenv("VISUAL_QA_FAIL_CLOSED") or "false").strip().lower()
        in {"1", "true", "yes", "sim", "on", "enabled", "enable"},
        "model": str(os.getenv("VISUAL_QA_MODEL") or "gpt-4.1-mini").strip() or "gpt-4.1-mini",
    }
