from __future__ import annotations

import os
from typing import Any, Dict, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def _resolve_endcard_aspect_ratio(branding: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    candidates = [
        branding.get("aspect_ratio"),
        kwargs.get("aspect_ratio"),
    ]
    opening_visual = kwargs.get("opening_visual")
    if isinstance(opening_visual, dict):
        candidates.extend([
            opening_visual.get("aspect_ratio"),
            opening_visual.get("ratio"),
        ])
    for candidate in candidates:
        value = str(candidate or "").strip().lower()
        if value in {"16:9", "9:16", "1:1", "4:5", "4:3", "3:4"}:
            return value
    return "16:9"


def install_final_video_presentation_guard(video_generator_cls: Type[Any]) -> Type[Any]:
    """Last-mile presentation hardening without creating a second renderer."""
    if getattr(video_generator_cls, "_codexia_final_video_presentation_guard_installed", False):
        return video_generator_cls

    original_endcard = getattr(video_generator_cls, "_resolve_closing_background_image", None)
    ensure_image = getattr(video_generator_cls, "_ensure_image_for_scene", None)
    if callable(original_endcard):
        def premium_endcard_background(
            self: Any,
            branding: Dict[str, Any],
            *args: Any,
            **kwargs: Any,
        ):
            if not _enabled("ENABLE_AI_PREMIUM_ENDCARD", "true"):
                return original_endcard(self, branding, *args, **kwargs)

            branding = branding if isinstance(branding, dict) else {}
            explicit = str(
                branding.get("closing_image_path")
                or branding.get("closing_image")
                or branding.get("closing_image_url")
                or ""
            ).strip()
            if explicit:
                resolved = original_endcard(self, branding, *args, **kwargs)
                if isinstance(resolved, dict) and resolved.get("path"):
                    return resolved

            aspect_ratio = _resolve_endcard_aspect_ratio(branding, kwargs)
            format_hint = {
                "9:16": "vertical portrait composition",
                "1:1": "square composition",
                "4:5": "vertical social composition",
                "3:4": "vertical portrait composition",
                "4:3": "classic landscape composition",
            }.get(aspect_ratio, "widescreen cinematic composition")

            if callable(ensure_image):
                prompt = (
                    "Premium cinematic Christian devotional end card background, no people, no faces, no text, no logo. "
                    f"{format_hint}, peaceful dawn horizon with subtle volumetric light, refined deep blue and warm gold tonal contrast, "
                    "elegant atmospheric depth, restrained filmic composition, clean central negative space for title and logo, "
                    "professional YouTube documentary finish, photorealistic, no letters, no typography."
                )
                try:
                    generated = ensure_image(
                        self,
                        prompt,
                        text_fallback="Encerramento",
                        aspect_ratio=aspect_ratio,
                    )
                    if generated and os.path.exists(str(generated)):
                        return {
                            "path": str(generated),
                            "source": "generated_premium_endcard_ai",
                            "aspect_ratio": aspect_ratio,
                        }
                except Exception:
                    pass

            resolved = original_endcard(self, branding, *args, **kwargs)
            if isinstance(resolved, dict):
                payload = dict(resolved)
                if str(payload.get("source") or "").lower() == "last_scene":
                    payload["path"] = None
                    payload["source"] = "dedicated_premium_endcard_fallback"
                payload.setdefault("aspect_ratio", aspect_ratio)
                return payload
            return {
                "path": None,
                "source": "dedicated_premium_endcard_fallback",
                "aspect_ratio": aspect_ratio,
            }

        video_generator_cls._resolve_closing_background_image = premium_endcard_background

    video_generator_cls._codexia_final_video_presentation_guard_installed = True
    return video_generator_cls
