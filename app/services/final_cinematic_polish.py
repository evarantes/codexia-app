from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def _semantic_caption_units(text: str, *, max_words: int = 6, max_chars: int = 40) -> List[str]:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if not clean:
        return []

    # Mantém pontuação com o bloco e prefere cortes em limites naturais.
    clauses = [p.strip() for p in re.findall(r"[^,;:.!?…]+(?:[,;:.!?…]+|$)", clean) if p.strip()]
    units: List[str] = []
    current: List[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            units.append(" ".join(current).strip())
            current = []

    for clause in clauses or [clean]:
        words = clause.split()
        for word in words:
            candidate = " ".join(current + [word]).strip()
            if current and (len(current) >= max_words or len(candidate) > max_chars):
                flush()
            current.append(word)
        # Pontuação forte é um bom ponto de respiração; vírgula pode continuar.
        if clause.endswith((".", "!", "?", "…", ";", ":")):
            flush()
    flush()

    # Evita fragmentos de uma palavra, causa mais visível de legenda amadora.
    i = 0
    while i < len(units):
        words = units[i].split()
        if len(words) == 1 and len(units) > 1:
            if i > 0:
                merged = f"{units[i - 1]} {units[i]}".strip()
                if len(merged.split()) <= max_words + 1 and len(merged) <= max_chars + 8:
                    units[i - 1] = merged
                    units.pop(i)
                    continue
            if i + 1 < len(units):
                merged = f"{units[i]} {units[i + 1]}".strip()
                if len(merged.split()) <= max_words + 1 and len(merged) <= max_chars + 8:
                    units[i] = merged
                    units.pop(i + 1)
                    continue
        i += 1
    return units


_VISUAL_DIVERSITY_CYCLE = (
    "Prioritize a wide environmental establishing image. Make the location, depth and atmosphere the main subject; keep recurring characters small when narration allows. Use cooler natural dawn or daylight rather than the previous warm portrait look.",
    "Prioritize visible physical action in a medium-wide composition. Change camera height and subject placement decisively; avoid centered consolation poses and static two-person portraits.",
    "Use a symbolic cutaway whenever the narration permits: environment, path, window light, water, sky, architecture or meaningful object as the primary subject. Do not force the recurring central character into this image.",
    "Use an intimate detail composition rather than another face-led portrait: hands, an object, fabric, footsteps, light, texture or a meaningful environmental detail. Keep human anatomy natural.",
    "Use an over-the-shoulder or layered foreground perspective with strong spatial depth. Move the scene to a clearly different background and use neutral realistic daylight instead of repeating golden interior lighting.",
    "Use a wide exterior composition with movement, journey or transition through the environment. Avoid the same room, same group arrangement and same camera distance as neighboring scenes.",
    "Use an asymmetrical quiet composition with meaningful negative space. If a person is required, place them off-center and secondary to the environment; use a distinct lighting direction and restrained palette.",
    "Use architecture, horizon, landscape or a strong environmental visual metaphor as the dominant image. Make this beat unmistakably different in location, framing and subject scale from the previous beat.",
)


def _local_premium_endcard(aspect_ratio: str) -> str | None:
    try:
        from PIL import Image, ImageDraw

        dims = {
            "9:16": (720, 1280),
            "1:1": (1080, 1080),
            "4:5": (864, 1080),
            "3:4": (810, 1080),
            "4:3": (1080, 810),
        }.get(str(aspect_ratio or "16:9"), (1280, 720))
        w, h = dims
        image = Image.new("RGB", dims)
        px = image.load()
        for y in range(h):
            t = y / max(1, h - 1)
            # Navy cinematográfico no alto, leve horizonte dourado no terço inferior.
            glow = max(0.0, 1.0 - abs(t - 0.72) / 0.28)
            r = int(8 + 26 * t + 38 * glow)
            g = int(18 + 24 * t + 24 * glow)
            b = int(40 + 30 * (1.0 - t) + 6 * glow)
            for x in range(w):
                px[x, y] = (min(255, r), min(255, g), min(255, b))
        draw = ImageDraw.Draw(image, "RGBA")
        # Luz suave no horizonte para dar leitura de encerramento sem texto embutido.
        for radius, alpha in ((int(w * .34), 12), (int(w * .24), 16), (int(w * .15), 22)):
            cx, cy = int(w * .50), int(h * .72)
            draw.ellipse((cx-radius, cy-radius//3, cx+radius, cy+radius//3), fill=(230, 183, 100, alpha))
        path = Path(tempfile.gettempdir()) / f"codexia_premium_endcard_{w}x{h}.png"
        image.save(path, format="PNG", optimize=True)
        return str(path)
    except Exception:
        return None


def install_final_cinematic_polish(video_generator_cls: Type[Any]) -> Type[Any]:
    """Última camada, focada somente em variedade visual, legendas e encerramento."""
    if getattr(video_generator_cls, "_codexia_final_cinematic_polish_installed", False):
        return video_generator_cls

    original_split = getattr(video_generator_cls, "_split_caption_units", None)
    if callable(original_split):
        def semantic_split(self: Any, text: str, max_words: int = 8, max_chars: int = 54):
            if not _enabled("ENABLE_SEMANTIC_CAPTION_CHUNKS", "true"):
                return original_split(self, text, max_words=max_words, max_chars=max_chars)
            return _semantic_caption_units(
                text,
                max_words=min(6, max(3, int(max_words or 6))),
                max_chars=min(40, max(24, int(max_chars or 40))),
            )
        video_generator_cls._split_caption_units = semantic_split

    original_ensure = getattr(video_generator_cls, "_ensure_image_for_scene", None)
    if callable(original_ensure):
        def diverse_ensure(self: Any, prompt: str, *args: Any, **kwargs: Any):
            if not _enabled("ENABLE_STRONG_VISUAL_DIVERSITY", "true"):
                return original_ensure(self, prompt, *args, **kwargs)
            raw = re.sub(r"\s+", " ", str(prompt or "").strip())
            lower = raw.lower()
            # Endcard tem direção própria; não mistura regras de cenas narrativas.
            if "end card" in lower or "endcard" in lower or "encerramento" in lower:
                return original_ensure(self, raw, *args, **kwargs)
            idx = int(getattr(self, "_codexia_visual_polish_index", 0) or 0)
            setattr(self, "_codexia_visual_polish_index", idx + 1)
            directive = _VISUAL_DIVERSITY_CYCLE[idx % len(_VISUAL_DIVERSITY_CYCLE)]
            strengthened = (
                f"{raw} Final cinematic diversity directive {idx + 1}: {directive} "
                "Do not reproduce the immediately previous composition, room, dominant color temperature, character grouping or camera distance. "
                "Preserve factual and character continuity only where narratively required; continuity must not become visual repetition."
            ).strip()
            return original_ensure(self, strengthened[:3800], *args, **kwargs)
        video_generator_cls._ensure_image_for_scene = diverse_ensure

    original_endcard = getattr(video_generator_cls, "_resolve_closing_background_image", None)
    if callable(original_endcard):
        def guaranteed_endcard(self: Any, branding: Dict[str, Any], *args: Any, **kwargs: Any):
            resolved = original_endcard(self, branding, *args, **kwargs)
            if not _enabled("ENABLE_GUARANTEED_DISTINCT_ENDCARD", "true"):
                return resolved
            if isinstance(resolved, dict):
                path = str(resolved.get("path") or "").strip()
                source = str(resolved.get("source") or "").lower()
                if path and os.path.exists(path) and source not in {"last_scene", "primary", "cover"}:
                    payload = dict(resolved)
                    payload["cinematic_polish"] = True
                    return payload
                aspect_ratio = str(resolved.get("aspect_ratio") or kwargs.get("aspect_ratio") or "16:9")
            else:
                aspect_ratio = str(kwargs.get("aspect_ratio") or "16:9")
            fallback = _local_premium_endcard(aspect_ratio)
            return {
                "path": fallback,
                "source": "local_guaranteed_premium_endcard" if fallback else "dedicated_premium_endcard_fallback",
                "aspect_ratio": aspect_ratio,
                "cinematic_polish": True,
            }
        video_generator_cls._resolve_closing_background_image = guaranteed_endcard

    video_generator_cls._codexia_final_cinematic_polish_installed = True
    return video_generator_cls
