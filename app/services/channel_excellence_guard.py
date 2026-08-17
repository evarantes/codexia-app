from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any, Dict, List, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def prepare_spoken_text(text: Any) -> str:
    """Small deterministic guard before TTS; preserves meaning and improves pt-BR clarity."""
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if not value:
        return value

    # Evita a expressão que no teste foi pronunciada como "peli contrário".
    value = re.sub(r"(?i)(?<!muito\s)\bpelo\s+contrário\b", "muito pelo contrário", value)

    # Nunca deixa uma ponte de abertura incompleta chegar à voz.
    value = re.sub(
        r"(?i)\b(?:uma|esta)\s+mensagem\s+de\s*(?:\.{2,}|[.!?,;:]|$)",
        "Esta mensagem é para você. ",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:uma|esta)\s+palavra\s+de\s*(?:\.{2,}|[.!?,;:]|$)",
        "Esta palavra é para você. ",
        value,
    )

    # Pontuação ajuda TTS a separar ideias sem alterar o conteúdo.
    value = re.sub(r"\s*([,;:.!?])\s*", r"\1 ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _clean_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def premium_endcard_lines(value: Any, max_lines: int = 2, max_chars: int = 46) -> List[str]:
    """Creates a short, readable mobile endcard without tiny paragraphs."""
    if isinstance(value, (list, tuple)):
        text = " ".join(_clean_line(item) for item in value if _clean_line(item))
    else:
        text = _clean_line(value)
    if not text:
        text = "Jesus continua presente. Leve esta esperança com você."

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if sentences:
        text = " ".join(sentences[:2])

    words = text.split()
    lines: List[str] = []
    current: List[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > max_chars:
            lines.append(" ".join(current).strip())
            current = [word]
            if len(lines) >= max_lines:
                break
        else:
            current.append(word)
    if current and len(lines) < max_lines:
        lines.append(" ".join(current).strip())

    if not lines:
        lines = ["Jesus continua presente."]
    if len(lines) == max_lines and len(" ".join(lines)) < len(text):
        lines[-1] = lines[-1].rstrip(" ,;:-")
        if not lines[-1].endswith((".", "!", "?")):
            lines[-1] += "."
    return lines[:max_lines]


def apply_channel_excellence_rollout() -> Dict[str, Any]:
    if "ENABLE_CHANNEL_EXCELLENCE_GUARD" not in os.environ:
        os.environ["ENABLE_CHANNEL_EXCELLENCE_GUARD"] = "true"
    return {
        "enabled": _enabled("ENABLE_CHANNEL_EXCELLENCE_GUARD", "true"),
        "tts_pronunciation_guard": True,
        "premium_endcard": True,
    }


def install_channel_excellence_guard_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    if getattr(video_generator_cls, "_codexia_channel_excellence_guard_installed", False):
        return video_generator_cls

    original_audio = getattr(video_generator_cls, "generate_audio", None)
    if callable(original_audio):
        def guarded_audio(self: Any, text: Any, *args: Any, **kwargs: Any):
            if not _enabled("ENABLE_CHANNEL_EXCELLENCE_GUARD", "true"):
                return original_audio(self, text, *args, **kwargs)
            return original_audio(self, prepare_spoken_text(text), *args, **kwargs)
        video_generator_cls.generate_audio = guarded_audio

    original_closing = getattr(video_generator_cls, "_resolve_contextual_closing", None)
    if callable(original_closing):
        def guarded_closing(self: Any, plan: Any = None):
            result = original_closing(self, plan)
            if not _enabled("ENABLE_CHANNEL_EXCELLENCE_GUARD", "true") or not isinstance(result, dict):
                return result
            payload = dict(result)
            payload["lines"] = premium_endcard_lines(payload.get("lines") or payload.get("message"))
            payload["premium_mobile_endcard"] = True
            return payload
        video_generator_cls._resolve_contextual_closing = guarded_closing

    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    if callable(original_create):
        def create_with_excellence_guard(self: Any, plan: Any, *args: Any, **kwargs: Any):
            if not _enabled("ENABLE_CHANNEL_EXCELLENCE_GUARD", "true") or not isinstance(plan, dict):
                return original_create(self, plan, *args, **kwargs)
            guarded = deepcopy(plan)
            closing_source = guarded.get("final_message") or guarded.get("closing_message")
            lines = premium_endcard_lines(closing_source)
            guarded["final_message"] = lines
            guarded["closing_message"] = " ".join(lines)
            guarded["endcard_cta_text"] = "Inscreva-se e acompanhe novas mensagens."
            branding = guarded.get("branding") if isinstance(guarded.get("branding"), dict) else {}
            branding = deepcopy(branding)
            branding["final_message"] = lines
            branding.setdefault("endcard_cta_text", guarded["endcard_cta_text"])
            guarded["branding"] = branding
            result = original_create(self, guarded, *args, **kwargs)
            if isinstance(result, dict):
                result["channel_excellence_guard"] = {
                    "enabled": True,
                    "endcard_lines": lines,
                    "endcard_cta_text": guarded["endcard_cta_text"],
                }
            return result
        video_generator_cls.create_video_from_plan = create_with_excellence_guard

    video_generator_cls._codexia_channel_excellence_guard_installed = True
    return video_generator_cls
