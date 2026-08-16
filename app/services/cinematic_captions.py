from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def apply_presentation_rollout() -> Dict[str, Any]:
    """Conservative presentation rollout with explicit environment rollback."""
    defaults = {
        "ENABLE_SCENE_DIRECTOR": "true",
        "ENABLE_CINEMATIC_CAPTIONS": "true",
    }
    for name, value in defaults.items():
        if name not in os.environ:
            os.environ[name] = value
    return {
        "scene_director_enabled": _enabled("ENABLE_SCENE_DIRECTOR", "true"),
        "cinematic_captions_enabled": _enabled("ENABLE_CINEMATIC_CAPTIONS", "true"),
    }


def install_cinematic_caption_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    """Makes narration captions smaller/cleaner while leaving opening/endcard APIs intact."""
    if getattr(video_generator_cls, "_codexia_cinematic_captions_installed", False):
        return video_generator_cls

    original_layout = getattr(video_generator_cls, "_caption_layout_metrics", None)
    original_split_units = getattr(video_generator_cls, "_split_caption_units", None)
    original_overlay = getattr(video_generator_cls, "create_text_overlay", None)
    if not callable(original_layout) or not callable(original_overlay):
        return video_generator_cls

    def premium_layout(
        self: Any,
        text: str,
        size=(1080, 1920),
        max_lines: int = 2,
        reserved_bottom_ratio: float = 0.0,
        safe_area_override=None,
    ):
        if not _enabled("ENABLE_CINEMATIC_CAPTIONS", "true"):
            return original_layout(
                self,
                text,
                size=size,
                max_lines=max_lines,
                reserved_bottom_ratio=reserved_bottom_ratio,
                safe_area_override=safe_area_override,
            )

        w, _h = size
        safe_area = {
            "top": 0.08,
            "bottom": max(0.065, float(reserved_bottom_ratio or 0.0)),
            "left": 0.075,
            "right": 0.075,
        }
        if isinstance(safe_area_override, dict):
            safe_area.update({k: float(v) for k, v in safe_area_override.items() if v is not None})

        layout_engine = self._build_safe_text_layout(size=size, safe_area=safe_area)
        base_size = max(22, min(52, int(w * 0.041)))
        min_size = max(16, min(25, int(w * 0.020)))
        metrics = layout_engine.fit_text_block(
            text=re.sub(r"\s+", " ", str(text or "").strip()),
            area=safe_area,
            preferred_font_size=base_size,
            min_font_size=min_size,
            max_lines=max_lines,
            line_spacing_ratio=1.16,
        )
        return {
            "fits": bool(metrics.get("fits")),
            "font": metrics.get("font"),
            "lines": list(metrics.get("lines") or []),
            "line_h": int(metrics.get("line_height") or 0),
            "font_size_used": int(metrics.get("font_size_used") or 0),
            "overflow_detected": bool(metrics.get("overflow_detected")),
            "layout": metrics,
            "cinematic_caption": True,
        }

    def premium_split_units(self: Any, text: str, max_words: int = 8, max_chars: int = 54) -> List[str]:
        if not callable(original_split_units) or not _enabled("ENABLE_CINEMATIC_CAPTIONS", "true"):
            if callable(original_split_units):
                return original_split_units(self, text, max_words=max_words, max_chars=max_chars)
            return [str(text or "").strip()] if str(text or "").strip() else []
        # Shorter phrase blocks reduce screen dominance without changing spoken narration.
        return original_split_units(
            self,
            text,
            max_words=min(int(max_words or 8), 7),
            max_chars=min(int(max_chars or 54), 46),
        )

    def premium_overlay(self: Any, text: str, *args: Any, **kwargs: Any):
        overlay = original_overlay(self, text, *args, **kwargs)
        if not _enabled("ENABLE_CINEMATIC_CAPTIONS", "true"):
            return overlay
        anchor = str(kwargs.get("vertical_anchor") or "bottom").strip().lower()
        footer = str(kwargs.get("footer_text") or "").strip()
        # Opening titles and branded endcards keep their existing placement.
        if anchor != "bottom" or footer:
            return overlay
        try:
            import numpy as np

            arr = np.asarray(overlay)
            if arr.ndim != 3 or arr.shape[0] <= 0:
                return overlay
            shift = max(2, int(arr.shape[0] * 0.018))
            shifted = np.zeros_like(arr)
            shifted[shift:] = arr[:-shift]
            return shifted
        except Exception:
            return overlay

    video_generator_cls._caption_layout_metrics = premium_layout
    if callable(original_split_units):
        video_generator_cls._split_caption_units = premium_split_units
    video_generator_cls.create_text_overlay = premium_overlay
    video_generator_cls._codexia_cinematic_captions_installed = True
    return video_generator_cls
