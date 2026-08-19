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


_MODE_DEFAULTS = {
    "economy": {"images_per_minute": 5.0, "quality": "low", "unit_cost_usd": 0.013},
    "balanced": {"images_per_minute": 8.0, "quality": "medium", "unit_cost_usd": 0.05},
    "premium": {"images_per_minute": 10.0, "quality": "high", "unit_cost_usd": 0.20},
}


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_mode(mode: str) -> str:
    value = str(mode or "balanced").strip().lower()
    aliases = {
        "economico": "economy",
        "econômico": "economy",
        "equilibrado": "balanced",
        "cinematico": "premium",
        "cinemático": "premium",
    }
    value = aliases.get(value, value)
    return value if value in _MODE_DEFAULTS else "balanced"


def estimate_video_cost(
    duration_minutes: float,
    *,
    mode: str = "balanced",
    model: str | None = None,
    regeneration_rate: float = 0.10,
    fixed_cost_usd: float | None = None,
) -> VideoCostEstimate:
    """Pre-generation estimate. It is intentionally conservative, not a billing invoice.

    Unit costs are configurable because GPT Image billing depends on model, quality,
    size and generated image-token usage. The defaults are reference values for the
    Codexia UI until real usage is recorded per operation.
    """
    minutes = max(0.25, float(duration_minutes or 0.0))
    normalized_mode = _normalize_mode(mode)
    cfg = dict(_MODE_DEFAULTS[normalized_mode])

    images_per_minute = _safe_float(
        os.getenv(f"CODEXIA_{normalized_mode.upper()}_IMAGES_PER_MINUTE"),
        cfg["images_per_minute"],
    )
    image_unit_cost = _safe_float(
        os.getenv(f"CODEXIA_{normalized_mode.upper()}_IMAGE_COST_USD"),
        cfg["unit_cost_usd"],
    )
    fixed = _safe_float(
        fixed_cost_usd if fixed_cost_usd is not None else os.getenv("CODEXIA_VIDEO_FIXED_COST_USD"),
        0.10,
    )

    base_images = max(1, int(math.ceil(minutes * max(1.0, images_per_minute))))
    regens = max(0, int(math.ceil(base_images * max(0.0, min(1.0, float(regeneration_rate or 0.0))))))
    endcards = 1
    billable_images = base_images + regens + endcards
    variable = round(billable_images * max(0.0, image_unit_cost), 6)
    total = round(max(0.0, fixed) + variable, 6)

    quality = str(os.getenv("OPENAI_IMAGE_QUALITY") or cfg["quality"]).strip().lower()
    if quality not in {"low", "medium", "high", "auto"}:
        quality = cfg["quality"]

    return VideoCostEstimate(
        duration_minutes=round(minutes, 3),
        mode=normalized_mode,
        provider="openai",
        model=str(model or os.getenv("CODEXIA_OPENAI_IMAGE_MODEL") or "gpt-image-2").strip(),
        image_quality=quality,
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
