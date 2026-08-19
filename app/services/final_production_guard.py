from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, Iterable, Optional, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).strip()


def prepare_ptbr_tts_text(text: Any) -> str:
    """Normaliza somente a fronteira TTS; roteiro e legenda não são alterados."""
    value = str(text or "")
    value = re.sub(r"(?i)\bJ[êé]zus\b", "Jesus", value)
    value = re.sub(r"(?i)\bJezus\b", "Jesus", value)
    return value


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


def _has_channel_cta(text: str) -> bool:
    low = _clean(text).lower()
    if not low:
        return False
    signals = ("inscreva", "curta", "compartilh", "canal")
    return sum(1 for token in signals if token in low) >= 2


def _has_natural_closure(text: str) -> bool:
    value = _clean(text)
    if len(value.split()) < 8:
        return False
    if value.endswith((",", ";", ":", "-", "—", "–")):
        return False
    return value.endswith((".", "!", "?"))


def _closing_sentence(plan: Dict[str, Any], scenes: Iterable[Dict[str, Any]]) -> str:
    for key in ("closing_message", "final_message", "end_message", "reflection_text"):
        candidate = _clean(plan.get(key))
        if candidate and not _has_channel_cta(candidate):
            return candidate[:220]
    scene_list = list(scenes)
    if scene_list:
        tail = _scene_text(scene_list[-1])
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", tail) if part.strip()]
        if sentences:
            return sentences[-1][:220]
    return "Leve esta verdade com você: Deus continua presente, e a sua resposta de fé pode começar hoje."


def _narrated_cta(plan: Dict[str, Any]) -> str:
    configured = _clean(plan.get("narrated_cta_text") or "")
    if configured:
        return configured[:220]
    return "Se esta mensagem falou com você, curta, inscreva-se no canal e compartilhe com alguém que precisa ouvi-la."


def ensure_narrated_closing(plan: Any) -> Any:
    """Garante conclusão e CTA DENTRO da narração, nunca como reflexão muda."""
    if not isinstance(plan, dict):
        return plan
    payload = copy.deepcopy(plan)
    scenes = [item for item in (payload.get("scenes") or []) if isinstance(item, dict)]
    if not scenes:
        return payload

    all_text = " ".join(_scene_text(scene) for scene in scenes)
    tail_text = _scene_text(scenes[-1])
    needs_cta = not _has_channel_cta(all_text)
    needs_closure = not _has_natural_closure(tail_text)
    closing = _closing_sentence(payload, scenes)
    cta = _narrated_cta(payload)

    final_parts = []
    if closing and (closing.lower() not in all_text.lower() or needs_closure):
        final_parts.append(closing)
    if needs_cta:
        final_parts.append(cta)

    if final_parts:
        template = copy.deepcopy(scenes[-1])
        key = _scene_text_key(template)
        template[key] = " ".join(final_parts).strip()
        for image_key in (
            "image_path", "image_url", "selected_image", "selected_image_path",
            "generated_image", "generated_image_path", "asset_path", "path",
        ):
            template.pop(image_key, None)
        closing_visual = (
            "Cinematic closing beat for a Christian devotional: a clearly distinct calm environment, "
            "hopeful but restrained, strong depth, no centered portrait, no repeated golden Jesus close-up, "
            "no text inside the image, visual sense of resolution and peace."
        )
        for prompt_key in ("image_prompt", "visual_prompt", "prompt"):
            if prompt_key in template:
                template[prompt_key] = closing_visual
        if not any(k in template for k in ("image_prompt", "visual_prompt", "prompt")):
            template["image_prompt"] = closing_visual
        template["codexia_narrated_closing"] = True
        scenes.append(template)

    payload["scenes"] = scenes
    payload["reflection_text"] = ""
    payload["closing_message"] = ""
    payload["final_message"] = ""
    payload["end_message"] = ""
    payload["cta"] = ""
    payload["cta_text"] = ""
    payload["endcard_cta_text"] = "Inscreva-se • Curta • Compartilhe"
    payload["codexia_narrated_closing_applied"] = bool(final_parts)
    return payload


def _extract_path(value: Any) -> Optional[str]:
    if isinstance(value, str):
        path = value.strip()
        return path if path and os.path.exists(path) else None
    if isinstance(value, dict):
        for key in ("path", "image_path", "file_path", "local_path", "generated_path"):
            found = _extract_path(value.get(key))
            if found:
                return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _extract_path(item)
            if found:
                return found
    return None


def _image_fingerprint(path: str) -> Optional[tuple[int, tuple[int, ...]]]:
    try:
        from PIL import Image
        image = Image.open(path).convert("RGB")
        small = image.convert("L").resize((9, 8))
        px = list(small.getdata())
        bits = 0
        bit_idx = 0
        for row in range(8):
            base = row * 9
            for col in range(8):
                if px[base + col] > px[base + col + 1]:
                    bits |= 1 << bit_idx
                bit_idx += 1
        hist = image.resize((32, 32)).histogram()
        buckets = []
        for channel in range(3):
            segment = hist[channel * 256:(channel + 1) * 256]
            for start in range(0, 256, 32):
                buckets.append(sum(segment[start:start + 32]))
        total = max(1, sum(buckets))
        normalized = tuple(int(round(v * 1000 / total)) for v in buckets)
        return bits, normalized
    except Exception:
        return None


def _hamming(a: int, b: int) -> int:
    return int((a ^ b).bit_count())


def _hist_distance(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    return sum(abs(x - y) for x, y in zip(a, b))


def _too_similar(fp: Optional[tuple[int, tuple[int, ...]]], previous: list[tuple[int, tuple[int, ...]]]) -> bool:
    if fp is None:
        return False
    return any(
        _hamming(fp[0], old[0]) <= 8 and _hist_distance(fp[1], old[1]) <= 90
        for old in previous[-6:]
    )


_RADICAL_DIVERSITY = (
    "REGENERATION FOR VISUAL UNIQUENESS: make this frame unmistakably different from every previous frame. "
    "Change location, camera distance, camera height, subject placement, dominant color temperature and action. "
    "Avoid a centered bearded-man portrait, avoid the recurring amber/golden interior, and avoid static groups facing camera. "
    "Prefer environmental storytelling, asymmetrical framing, physical action or a symbolic cutaway when faithful to the narration. "
    "No text in the image."
)


def _with_diversity_prompt(prompt: str) -> str:
    return (f"{_clean(prompt)} {_RADICAL_DIVERSITY}").strip()[:3900]


def install_final_production_guard(video_generator_cls: Type[Any]) -> Type[Any]:
    """Camada final de produção: TTS, conclusão narrada e anti-repetição real."""
    if getattr(video_generator_cls, "_codexia_final_production_guard_installed", False):
        return video_generator_cls

    try:
        from app.services import channel_excellence_guard as excellence
        excellence._tts_pronunciation = prepare_ptbr_tts_text
    except Exception:
        pass

    original_create = getattr(video_generator_cls, "create_video", None)
    if callable(original_create):
        def guarded_create(self: Any, plan: Any, *args: Any, **kwargs: Any):
            final_plan = ensure_narrated_closing(plan) if _enabled("ENABLE_NARRATED_CLOSING", "true") else plan
            return original_create(self, final_plan, *args, **kwargs)
        video_generator_cls.create_video = guarded_create

    original_compose = getattr(video_generator_cls, "compose_video", None)
    if callable(original_compose):
        def guarded_compose(self: Any, *args: Any, **kwargs: Any):
            if _enabled("DISABLE_SILENT_FINAL_REFLECTION", "true"):
                for key in ("reflection_text", "closing_message", "cta_text", "final_message", "end_message"):
                    if key in kwargs:
                        kwargs[key] = None
            return original_compose(self, *args, **kwargs)
        video_generator_cls.compose_video = guarded_compose

    original_ensure = getattr(video_generator_cls, "_ensure_image_for_scene", None)
    if callable(original_ensure):
        def uniqueness_ensure(self: Any, prompt: str, *args: Any, **kwargs: Any):
            result = original_ensure(self, prompt, *args, **kwargs)
            if not _enabled("ENABLE_PERCEPTUAL_IMAGE_DEDUP", "true"):
                return result
            path = _extract_path(result)
            fp = _image_fingerprint(path) if path else None
            previous = list(getattr(self, "_codexia_visual_fingerprints", []) or [])
            if fp is not None and _too_similar(fp, previous):
                retry_result = original_ensure(self, _with_diversity_prompt(prompt), *args, **kwargs)
                retry_path = _extract_path(retry_result)
                retry_fp = _image_fingerprint(retry_path) if retry_path else None
                if retry_fp is not None and not _too_similar(retry_fp, previous):
                    result, fp = retry_result, retry_fp
                elif retry_fp is not None and fp is not None:
                    old_best = min((_hamming(fp[0], old[0]) for old in previous[-6:]), default=64)
                    retry_best = min((_hamming(retry_fp[0], old[0]) for old in previous[-6:]), default=64)
                    if retry_best > old_best:
                        result, fp = retry_result, retry_fp
            if fp is not None:
                previous.append(fp)
                setattr(self, "_codexia_visual_fingerprints", previous[-12:])
            return result
        video_generator_cls._ensure_image_for_scene = uniqueness_ensure

    video_generator_cls._codexia_final_production_guard_installed = True
    return video_generator_cls


def install_openai_quality_policy_override() -> bool:
    """Qualidade final usa OpenAI GPT Image 2 mesmo se a política persistida ainda disser mini."""
    if not _enabled("CODEXIA_FORCE_OPENAI_FINAL_IMAGES", "true"):
        return False
    try:
        from app.services.ai_router import AIRouter, AICapability, AIPolicy
    except Exception:
        return False
    if getattr(AIRouter, "_codexia_openai_quality_override_installed", False):
        return True
    original_load = AIRouter._load_policy

    def quality_load(self: Any, db: Any, *, user_id: Optional[int], capability: str, settings: Any):
        policy = original_load(self, db, user_id=user_id, capability=capability, settings=settings)
        if str(capability) not in {AICapability.IMAGE_GENERATION, AICapability.THUMBNAIL_GENERATION}:
            return policy
        model = str(os.getenv("CODEXIA_OPENAI_IMAGE_MODEL") or "gpt-image-2").strip() or "gpt-image-2"
        try:
            estimated = float(os.getenv("OPENAI_IMAGE_ESTIMATED_COST_USD") or 0.05)
        except Exception:
            estimated = 0.05
        return AIPolicy(
            capability=str(capability),
            primary_provider="openai",
            primary_model=model,
            fallback_enabled=False,
            fallback_provider=None,
            fallback_model=None,
            cache_enabled=False,
            estimated_cost=max(0.0, estimated),
            max_cost=policy.max_cost,
            is_active=policy.is_active,
        )

    AIRouter._load_policy = quality_load
    AIRouter._codexia_openai_quality_override_installed = True
    return True
