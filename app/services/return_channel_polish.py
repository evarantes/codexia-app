from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).strip()


def _scene_text(scene: Dict[str, Any]) -> str:
    for key in ("text", "narration_text", "narration", "content"):
        value = _clean(scene.get(key))
        if value:
            return value
    return ""


def _scene_text_key(scene: Dict[str, Any]) -> str:
    for key in ("text", "narration_text", "narration", "content"):
        if key in scene:
            return key
    return "text"


def _cta_signals(text: str) -> set[str]:
    folded = _clean(text).lower()
    signals: set[str] = set()
    if "inscreva" in folded or "inscrição" in folded or "inscricao" in folded:
        signals.add("subscribe")
    if "sininho" in folded or "notifica" in folded:
        signals.add("bell")
    if "compartilh" in folded:
        signals.add("share")
    if "curta" in folded or "like" in folded:
        signals.add("like")
    return signals


def _has_complete_channel_cta(text: str) -> bool:
    signals = _cta_signals(text)
    return {"subscribe", "bell", "share"}.issubset(signals)


def _default_narrated_cta() -> str:
    return (
        "Se esta mensagem falou com você, inscreva-se no canal, ative o sininho "
        "para receber as próximas mensagens e compartilhe este vídeo com alguém que precisa ouvi-lo."
    )


def _closing_visual_prompt() -> str:
    return (
        "Cinematic final beat for a premium Christian devotional video. A clearly distinct, peaceful "
        "environment that communicates completion, hope and spiritual resolution; natural depth, refined "
        "light, elegant widescreen composition, no text inside the image, no repeated portrait, no duplicated "
        "characters, no static group facing camera. Leave clean visual space for the final channel end card."
    )


def _append_narrated_cta_scene(scenes: list[Dict[str, Any]], cta_text: str) -> None:
    template = copy.deepcopy(scenes[-1])
    text_key = _scene_text_key(template)
    template[text_key] = _clean(cta_text) or _default_narrated_cta()

    # Nunca reutiliza a arte/path da cena anterior para o CTA final.
    for key in (
        "image_path", "image_url", "selected_image", "selected_image_path",
        "generated_image", "generated_image_path", "asset_path", "path", "image",
    ):
        template.pop(key, None)
    template["image_prompt"] = _closing_visual_prompt()
    template.pop("visual_prompt", None)
    template.pop("prompt", None)
    template["codexia_narrated_channel_cta"] = True
    scenes.append(template)


def ensure_narrated_return_cta(plan: Any) -> Any:
    """Garante CTA falado como última cena real, sem duplicar o que já existe.

    A conclusão espiritual continua pertencendo ao Editor Narrativo. Esta camada
    acrescenta somente o convite operacional do canal (inscrição, sininho e
    compartilhamento) e o coloca dentro da timeline normal de narração/legenda.
    """
    if not isinstance(plan, dict):
        return plan

    payload = copy.deepcopy(plan)
    scenes = [item for item in (payload.get("scenes") or []) if isinstance(item, dict)]
    if not scenes:
        return payload

    scene_narration = " ".join(_scene_text(scene) for scene in scenes)
    legacy_candidates = [
        _clean(payload.get(key))
        for key in ("narrated_cta_text", "cta_text", "closing_text")
        if _clean(payload.get(key))
    ]
    complete_legacy_cta = next((text for text in legacy_candidates if _has_complete_channel_cta(text)), "")

    if _has_complete_channel_cta(scene_narration):
        payload["codexia_narrated_channel_cta_applied"] = False
    else:
        _append_narrated_cta_scene(scenes, complete_legacy_cta or _default_narrated_cta())
        payload["codexia_narrated_channel_cta_applied"] = True

    payload["scenes"] = scenes

    # O CTA já está dentro da narração principal; impede um segundo bloco falado
    # separado e a pausa artificial que existia antes dele.
    payload["cta_text"] = ""
    payload["narrated_cta_text"] = ""
    payload["closing_text"] = ""
    payload["endcard_cta_text"] = "INSCREVA-SE • ATIVE O SININHO • COMPARTILHE"
    payload.setdefault("end_screen_target_duration_sec", 1.2)
    payload.setdefault("pause_duration_sec", 0.0)
    return payload


def install_return_channel_polish(video_generator_cls: Type[Any]) -> Type[Any]:
    """Última preparação do plano antes do renderer canônico."""
    if getattr(video_generator_cls, "_codexia_return_channel_polish_installed", False):
        return video_generator_cls

    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    if not callable(original_create):
        return video_generator_cls

    def create_with_return_polish(self: Any, plan: Any, *args: Any, **kwargs: Any):
        if not _enabled("ENABLE_RETURN_CHANNEL_POLISH", "true"):
            return original_create(self, plan, *args, **kwargs)
        polished = ensure_narrated_return_cta(plan)
        return original_create(self, polished, *args, **kwargs)

    video_generator_cls.create_video_from_plan = create_with_return_polish
    video_generator_cls._codexia_return_channel_polish_installed = True
    return video_generator_cls


__all__ = ["ensure_narrated_return_cta", "install_return_channel_polish"]
