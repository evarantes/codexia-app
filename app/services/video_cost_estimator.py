from __future__ import annotations

import math
import os
from dataclasses import dataclass, asdict
from typing import Dict, Any


@dataclass(frozen=True)
class VideoCostEstimate:
    duration_minutes: float
    mode: str
    provider: str
    model: str
    image_quality: str
    images_per_minute: float
    estimated_images: int
    estimated_regenerations: int
    estimated_endcards: int
    image_unit_cost_usd: float
    fixed_cost_usd: float
    variable_cost_usd: float
    total_cost_usd: float
    cost_per_minute_usd: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# A cadência visual fica igual entre os perfis para não reduzir a qualidade
# editorial do vídeo. O que muda é a qualidade solicitada ao GPT Image e,
# consequentemente, o custo estimado por imagem. Tudo continua configurável
# por ambiente sem hard-code de preço como fonte de verdade.
_MODE_DEFAULTS = {
    "economy": {"images_per_minute": 8.0, "quality": "low", "unit_cost_usd": 0.013},
    "balanced": {"images_per_minute": 8.0, "quality": "medium", "unit_cost_usd": 0.05},
    "premium": {"images_per_minute": 8.0, "quality": "high", "unit_cost_usd": 0.20},
}


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def normalize_mode(mode: str) -> str:
    value = str(mode or "balanced").strip().lower()
    aliases = {
        "economico": "economy",
        "econômico": "economy",
        "equilibrado": "balanced",
        "cinematico": "premium",
        "cinemático": "premium",
        "qualidade_maxima": "premium",
        "qualidade máxima": "premium",
        "maximum": "premium",
    }
    value = aliases.get(value, value)
    return value if value in _MODE_DEFAULTS else "balanced"


def image_profile_for_mode(mode: str) -> Dict[str, Any]:
    normalized_mode = normalize_mode(mode)
    cfg = dict(_MODE_DEFAULTS[normalized_mode])
    images_per_minute = _safe_float(
        os.getenv(f"CODEXIA_{normalized_mode.upper()}_IMAGES_PER_MINUTE"),
        cfg["images_per_minute"],
    )
    image_unit_cost = _safe_float(
        os.getenv(f"CODEXIA_{normalized_mode.upper()}_IMAGE_COST_USD"),
        cfg["unit_cost_usd"],
    )
    quality = str(
        os.getenv(f"CODEXIA_{normalized_mode.upper()}_IMAGE_QUALITY")
        or cfg["quality"]
    ).strip().lower()
    if quality not in {"low", "medium", "high", "auto"}:
        quality = str(cfg["quality"])
    return {
        "mode": normalized_mode,
        "provider": "openai",
        "model": str(os.getenv("CODEXIA_OPENAI_IMAGE_MODEL") or "gpt-image-2").strip(),
        "image_quality": quality,
        "images_per_minute": max(1.0, images_per_minute),
        "image_unit_cost_usd": max(0.0, image_unit_cost),
    }


def estimate_video_cost(
    duration_minutes: float,
    *,
    mode: str = "balanced",
    model: str | None = None,
    regeneration_rate: float = 0.10,
    fixed_cost_usd: float | None = None,
) -> VideoCostEstimate:
    """Estimativa prévia conservadora; não representa a fatura oficial.

    Os custos unitários são configuráveis porque a cobrança do GPT Image pode
    variar por modelo, qualidade, tamanho e tokens de imagem. O Codexia usa
    esses valores para decidir antes de gastar e depois compara com as
    operações efetivamente registradas durante a produção.
    """
    minutes = max(0.25, float(duration_minutes or 0.0))
    profile = image_profile_for_mode(mode)
    normalized_mode = str(profile["mode"])
    images_per_minute = float(profile["images_per_minute"])
    image_unit_cost = float(profile["image_unit_cost_usd"])
    fixed = _safe_float(
        fixed_cost_usd if fixed_cost_usd is not None else os.getenv("CODEXIA_VIDEO_FIXED_COST_USD"),
        0.10,
    )

    base_images = max(1, int(math.ceil(minutes * images_per_minute)))
    regens = max(0, int(math.ceil(base_images * max(0.0, min(1.0, float(regeneration_rate or 0.0))))))
    endcards = 1
    billable_images = base_images + regens + endcards
    variable = round(billable_images * image_unit_cost, 6)
    total = round(max(0.0, fixed) + variable, 6)

    return VideoCostEstimate(
        duration_minutes=round(minutes, 3),
        mode=normalized_mode,
        provider="openai",
        model=str(model or profile["model"]).strip(),
        image_quality=str(profile["image_quality"]),
        images_per_minute=round(images_per_minute, 3),
        estimated_images=base_images,
        estimated_regenerations=regens,
        estimated_endcards=endcards,
        image_unit_cost_usd=round(image_unit_cost, 6),
        fixed_cost_usd=round(max(0.0, fixed), 6),
        variable_cost_usd=variable,
        total_cost_usd=total,
        cost_per_minute_usd=round(total / minutes, 6),
    )


def project_from_baseline(
    baseline_duration_minutes: float,
    baseline_total_cost_usd: float,
    target_duration_minutes: float,
    *,
    fixed_cost_usd: float = 0.10,
) -> Dict[str, float]:
    base_minutes = max(0.25, float(baseline_duration_minutes or 0.0))
    target_minutes = max(0.25, float(target_duration_minutes or 0.0))
    fixed = max(0.0, float(fixed_cost_usd or 0.0))
    baseline = max(0.0, float(baseline_total_cost_usd or 0.0))
    variable_baseline = max(0.0, baseline - fixed)
    variable_per_minute = variable_baseline / base_minutes
    projected = fixed + (variable_per_minute * target_minutes)
    return {
        "baseline_duration_minutes": round(base_minutes, 3),
        "baseline_total_cost_usd": round(baseline, 6),
        "fixed_cost_usd": round(fixed, 6),
        "variable_cost_per_minute_usd": round(variable_per_minute, 6),
        "target_duration_minutes": round(target_minutes, 3),
        "projected_total_cost_usd": round(projected, 6),
    }


__all__ = [
    "VideoCostEstimate",
    "estimate_video_cost",
    "project_from_baseline",
    "image_profile_for_mode",
    "normalize_mode",
]
