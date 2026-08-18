from __future__ import annotations

import os
from typing import Any, Dict, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


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

            if callable(ensure_image):
                prompt = (
                    "Premium cinematic Christian devotional end card background, no people, no faces, no text, no logo. "
                    "Peaceful dawn horizon with subtle volumetric light, refined deep blue and warm gold tonal contrast, "
                    "elegant atmospheric depth, restrained filmic composition, clean central negative space for title and logo, "
                    "professional YouTube documentary finish, photorealistic, 16:9, no letters, no typography."
                )
                try:
                    generated = ensure_image(
                        self,
                        prompt,
                        text_fallback="Encerramento",
                        aspect_ratio="16:9",
                    )
                    if generated and os.path.exists(str(generated)):
                        return {
                            "path": str(generated),
                            "source": "generated_premium_endcard_ai",
                        }
                except Exception:
                    pass

            resolved = original_endcard(self, branding, *args, **kwargs)
            if isinstance(resolved, dict):
                payload = dict(resolved)
                if str(payload.get("source") or "").lower() == "last_scene":
                    payload["path"] = None
                    payload["source"] = "dedicated_premium_endcard_fallback"
                return payload
            return {"path": None, "source": "dedicated_premium_endcard_fallback"}

        video_generator_cls._resolve_closing_background_image = premium_endcard_background

    video_generator_cls._codexia_final_video_presentation_guard_installed = True
    return video_generator_cls
