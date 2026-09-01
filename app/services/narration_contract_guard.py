from __future__ import annotations

import html
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Type

from app.services.video_creation_standard import (
    STANDARD_COMPLETE_CTA,
    STANDARD_REQUIRED_CTA_SIGNALS,
    apply_standard_video_structure,
)


DEFAULT_COMPLETE_CTA = STANDARD_COMPLETE_CTA

# Markup de pausa é metadado de direção, nunca conteúdo narrável. Alguns provedores
# aceitam SSML e outros escapam a tag, fazendo a voz ler literalmente "break time".
# O contrato canônico do Codexia é texto puro: pausas são convertidas em pontuação
# ANTES do último guard que antecede qualquer chamada paga de TTS.
_PAUSE_MARKUP_PATTERNS = (
    re.compile(r"(?is)<\s*break\b[^>]*?/?>"),
    re.compile(
        r"(?ix)\[\s*(?:break|pause|pausa)\s*(?:time|duration|tempo|dura[cç][aã]o)?\s*[:=]?\s*"
        r"[\"']?\d+(?:[.,]\d+)?\s*(?:ms|s|sec|secs|seconds?|segundos?)?[\"']?\s*\]"
    ),
    re.compile(
        r"(?ix)\(\s*(?:break|pause|pausa)\s*(?:time|duration|tempo|dura[cç][aã]o)?\s*[:=]?\s*"
        r"[\"']?\d+(?:[.,]\d+)?\s*(?:ms|s|sec|secs|seconds?|segundos?)?[\"']?\s*\)"
    ),
    re.compile(
        r"(?ix)\b(?:break\s*time|pause\s*(?:time|duration)?|pausa\s*(?:tempo|dura[cç][aã]o)?)\s*[:=]\s*"
        r"[\"']?\d+(?:[.,]\d+)?\s*(?:ms|s|sec|secs|seconds?|segundos?)?[\"']?\s*/?>?"
    ),
)

# Tags SSML de contêiner/ênfase não carregam palavras narráveis por si mesmas.
# Removê-las preserva o texto interno e impede que provedores plain-text as leiam.
_SAFE_SSML_CONTAINER_TAG = re.compile(
    r"(?is)</?\s*(?:speak|prosody|voice|p|s|emphasis|sub|phoneme)\b[^>]*>"
)

_STRUCTURAL_PATTERNS = (
    ("code_fence", re.compile(r"```|~~~")),
    ("json_field", re.compile(
        r"(?i)(?:[\"']?(?:image_prompt|visual_prompt|camera_movement|motion_effect|scene_qc|"
        r"scene_card|narration_text|on_screen_text|selected_images|generated_image_path|"
        r"asset_path|render_report|prompt|metadata)[\"']?\s*:)"
    )),
    ("json_scene_field", re.compile(r"(?i)[\"']?scene[\"']?\s*:\s*(?:\d+|\{|\[|[\"'])")),
    ("json_object", re.compile(r"\{\s*[\"'][A-Za-z_][A-Za-z0-9_]*[\"']\s*:")),
    ("json_array_object", re.compile(r"\[\s*\{\s*[\"'][A-Za-z_][A-Za-z0-9_]*[\"']\s*:")),
    ("python_dict", re.compile(r"\{\s*'[A-Za-z_][A-Za-z0-9_]*'\s*:")),
    ("internal_label", re.compile(
        r"(?im)^\s*(?:image_prompt|visual_prompt|camera_movement|motion_effect|scene_qc|"
        r"scene_card|metadata|system_prompt|assistant|json|payload|render_report)\s*[:=]"
    )),
    ("technical_timing_assignment", re.compile(
        r"(?i)\b(?:break\s*time|pause(?:[_\s]*(?:time|duration))?|pausa(?:[_\s]*(?:tempo|dura[cç][aã]o))?|"
        r"start[_\s]*time|end[_\s]*time|timestamp|timecode|duration[_\s]*(?:sec|seconds)?|"
        r"scene[_\s]*id|segment[_\s]*id)\b\s*[:=]"
    )),
    ("template_placeholder", re.compile(r"\{\{[^{}]+\}\}|\$\{[^{}]+\}")),
    ("xml_or_ssml_residue", re.compile(r"(?is)<\s*/?\s*[A-Za-z][^>]*>")),
)


class NarrationContractError(RuntimeError):
    pass


def _fold(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", raw).strip().lower()


def _clean(text: Any) -> str:
    raw = unicodedata.normalize("NFKC", str(text or "")).replace("\x00", " ")
    return re.sub(r"\s+", " ", raw).strip()


def sanitize_narration_text(text: Any) -> str:
    """Converte somente markup editorial seguro em texto puro narrável.

    Não tenta "consertar" JSON/código arbitrário: qualquer resíduo técnico é
    detectado depois por ``structural_issues`` e bloqueia o TTS fail-closed.
    """
    raw = unicodedata.normalize("NFKC", html.unescape(str(text or ""))).replace("\x00", " ")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    for pattern in _PAUSE_MARKUP_PATTERNS:
        raw = pattern.sub(", ", raw)
    raw = _SAFE_SSML_CONTAINER_TAG.sub(" ", raw)
    # Normaliza pontuação criada pela remoção de pausas sem colar palavras.
    raw = re.sub(r"\s*,\s*,+\s*", ", ", raw)
    raw = re.sub(r"\s+([,;:.!?])", r"\1", raw)
    raw = re.sub(r"([,;:.!?])(?=[^\s\n\"”’')\]])", r"\1 ", raw)
    return re.sub(r"\s+", " ", raw).strip(" ,")


def cta_signals(text: Any) -> set[str]:
    folded = _fold(text)
    signals: set[str] = set()
    if "curta" in folded or "curtir" in folded or "deixe seu like" in folded or "deixe o like" in folded:
        signals.add("like")
    if "inscreva" in folded or "inscricao" in folded:
        signals.add("subscribe")
    if "sininho" in folded or "notifica" in folded:
        signals.add("bell")
    if "compartilh" in folded:
        signals.add("share")
    return signals


def has_complete_cta(text: Any) -> bool:
    return STANDARD_REQUIRED_CTA_SIGNALS.issubset(cta_signals(text))


def structural_issues(text: Any) -> List[str]:
    raw = str(text or "")
    issues = [name for name, pattern in _STRUCTURAL_PATTERNS if pattern.search(raw)]
    if re.search(r"\\[nrt]\s*[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?\s*:", raw):
        issues.append("escaped_serialized_field")
    return sorted(set(issues))


def validate_narration_text(
    text: Any,
    *,
    label: str = "narração",
    require_terminal_sentence: bool = True,
    require_complete_cta: bool = False,
) -> str:
    cleaned = sanitize_narration_text(text)
    if not cleaned:
        raise NarrationContractError(f"{label}: texto vazio; TTS bloqueado antes de qualquer chamada paga.")
    issues = structural_issues(cleaned)
    if issues:
        raise NarrationContractError(
            f"{label}: conteúdo estrutural/código detectado ({', '.join(issues)}); "
            "TTS bloqueado antes de qualquer chamada paga."
        )
    if require_terminal_sentence and not re.search(r"[.!?…][\"”’')\]]*$", cleaned):
        raise NarrationContractError(
            f"{label}: frase final parece truncada; TTS bloqueado antes de qualquer chamada paga."
        )
    if require_complete_cta and not has_complete_cta(cleaned):
        missing = sorted(STANDARD_REQUIRED_CTA_SIGNALS - cta_signals(cleaned))
        raise NarrationContractError(
            f"{label}: CTA incompleto ({', '.join(missing)}); TTS bloqueado antes de qualquer chamada paga."
        )
    return cleaned


def _scene_text(scene: Dict[str, Any]) -> str:
    for key in ("_tts_text", "text", "narration_text", "narration", "content"):
        value = sanitize_narration_text(scene.get(key))
        if value:
            return value
    return ""


def _estimate(self: Any, text: str, voice_style: Optional[str], voice_gender: Optional[str]) -> float:
    try:
        return float(self._estimate_text_duration_with_voice(
            text, voice_style=voice_style, voice_gender=voice_gender
        ) or 0.0)
    except Exception:
        return 0.0


def _pick_complete_cta(plan: Dict[str, Any], marked: List[str], fallback: Any = "") -> str:
    candidates = list(marked)
    for key in ("narrated_cta_text", "cta_text", "closing_text"):
        value = sanitize_narration_text(plan.get(key))
        if value:
            candidates.append(value)
    fallback_clean = sanitize_narration_text(fallback)
    if fallback_clean:
        candidates.append(fallback_clean)
    candidates.append(DEFAULT_COMPLETE_CTA)
    return next((value for value in candidates if has_complete_cta(value)), DEFAULT_COMPLETE_CTA)


def install_narration_contract_guard(video_generator_cls: Type[Any]) -> Type[Any]:
    """Fail-closed imediatamente antes do TTS e protege reflexão + CTA."""
    if getattr(video_generator_cls, "_codexia_narration_contract_guard_v1", False):
        return video_generator_cls

    original_prepare = getattr(video_generator_cls, "prepare_final_narration_text", None)
    original_generate_audio = getattr(video_generator_cls, "generate_audio", None)
    original_segmented = getattr(video_generator_cls, "_compose_segmented_narration_audio", None)

    if callable(original_prepare):
        def prepare_guarded(self: Any, plan: Optional[Dict[str, Any]], scenes: List[Dict[str, Any]], voice_style=None, voice_gender=None):
            if isinstance(plan, dict):
                # O perfil padrão é aplicado de forma aditiva ao próprio plano
                # para que música/legendas/abertura/fechamento sejam observados
                # também nas etapas posteriores do renderer. Campos explícitos
                # do pedido do usuário nunca são sobrescritos.
                apply_standard_video_structure(plan)
            safe_plan = dict(plan or {}) if isinstance(plan, dict) else {}
            marked_cta: List[str] = []
            if isinstance(scenes, list):
                body_scenes: List[Dict[str, Any]] = []
                for scene in list(scenes):
                    if isinstance(scene, dict) and bool(scene.get("codexia_narrated_channel_cta")):
                        text = _scene_text(scene)
                        if text:
                            marked_cta.append(text)
                        continue
                    body_scenes.append(scene)
                if len(body_scenes) != len(scenes):
                    scenes[:] = body_scenes

            meta = dict(original_prepare(
                self, safe_plan, scenes, voice_style=voice_style, voice_gender=voice_gender
            ) or {})
            raw_opening = _clean(meta.get("opening_text"))
            raw_body = _clean(meta.get("body_text"))
            raw_reflection = _clean(meta.get("reflection_text"))
            raw_cta = _clean(meta.get("cta_text") or meta.get("closing_text"))

            opening = validate_narration_text(raw_opening, label="abertura")
            body = validate_narration_text(raw_body, label="corpo da narração")
            reflection = validate_narration_text(raw_reflection, label="reflexão final") if raw_reflection else ""

            if reflection and body.endswith(reflection):
                body = body[:-len(reflection)].rstrip()
                if body and not re.search(r"[.!?…][\"”’')\]]*$", body):
                    body = body.rstrip(",;:-") + "."
                body = validate_narration_text(body, label="corpo da narração")

            cta = _pick_complete_cta(safe_plan, marked_cta, raw_cta)
            cta = validate_narration_text(cta, label="CTA final", require_complete_cta=True)
            full_text = " ".join(part for part in [opening, body, reflection, cta] if part).strip()
            full_text = validate_narration_text(full_text, label="narração final")

            meta["opening_text"] = opening
            meta["body_text"] = body
            meta["reflection_text"] = reflection
            meta["cta_text"] = cta
            meta["closing_text"] = cta
            meta["full_text"] = full_text
            meta["video_creation_standard"] = dict(safe_plan.get("codexia_video_standard") or {})
            try:
                meta["char_count"] = len(full_text)
                meta["word_count"] = int(self._count_words(full_text))
            except Exception:
                meta["char_count"] = len(full_text)
                meta["word_count"] = len(full_text.split())
            reflection_est = _estimate(self, reflection, voice_style, voice_gender) if reflection else 0.0
            cta_est = _estimate(self, cta, voice_style, voice_gender)
            meta["reflection_duration_est_sec"] = round(reflection_est, 2)
            meta["closing_duration_est_sec"] = round(cta_est, 2)
            meta["cta_duration_est_sec"] = round(cta_est, 2)
            meta["estimated_total_duration_sec"] = round(
                float(meta.get("intro_opening_hold_sec") or 0.0)
                + float(meta.get("opening_duration_est_sec") or 0.0)
                + float(meta.get("body_duration_est_sec") or 0.0)
                + reflection_est + cta_est
                + float(meta.get("pause_duration_sec") or 0.0),
                2,
            )
            sanitized_fields = []
            for name, before, after in (
                ("opening_text", raw_opening, opening),
                ("body_text", raw_body, body),
                ("reflection_text", raw_reflection, reflection),
                ("closing_text", raw_cta, cta),
            ):
                if before and sanitize_narration_text(before) != before:
                    sanitized_fields.append(name)
            meta["protected_closing_contract"] = {
                "version": 3,
                "reflection_protected": bool(reflection),
                "cta_protected": True,
                "cta_signals": sorted(cta_signals(cta)),
                "required_cta_signals": sorted(STANDARD_REQUIRED_CTA_SIGNALS),
                "structural_issues": structural_issues(full_text),
                "technical_markup_sanitized_fields": sorted(set(sanitized_fields)),
                "tts_plain_text_only": True,
                "tts_allowed": True,
            }
            return meta

        video_generator_cls.prepare_final_narration_text = prepare_guarded

    if callable(original_segmented):
        def segmented_guarded(self: Any, *, main_text: str, cta_text: str, **kwargs: Any):
            main_clean = validate_narration_text(main_text, label="narração principal")
            cta_clean = sanitize_narration_text(cta_text)
            if not has_complete_cta(cta_clean):
                cta_clean = DEFAULT_COMPLETE_CTA
            cta_clean = validate_narration_text(
                cta_clean, label="CTA final", require_complete_cta=True
            )
            return original_segmented(self, main_text=main_clean, cta_text=cta_clean, **kwargs)
        video_generator_cls._compose_segmented_narration_audio = segmented_guarded

    if callable(original_generate_audio):
        def generate_audio_guarded(self: Any, text: str, *args: Any, **kwargs: Any):
            raw = _clean(text)
            clean = validate_narration_text(raw, label="texto enviado ao TTS")
            try:
                debug = dict(getattr(self, "_last_tts_debug", {}) or {})
                debug["narration_contract_version"] = 3
                debug["tts_plain_text_only"] = True
                debug["technical_markup_sanitized"] = bool(raw != clean)
                debug["remaining_structural_issues"] = structural_issues(clean)
                self._last_tts_debug = debug
            except Exception:
                pass
            output = original_generate_audio(self, clean, *args, **kwargs)
            task_id = str(getattr(self, "_codexia_task_id", "") or "").strip()
            if task_id and output and isinstance(output, str) and os.path.isfile(output):
                try:
                    from app.services.production_manifest import record_artifact
                    record_artifact(task_id, output, kind="audio", source="tts_immediate")
                except Exception:
                    pass
            return output
        video_generator_cls.generate_audio = generate_audio_guarded

    video_generator_cls._codexia_narration_contract_guard_v1 = True
    video_generator_cls._codexia_narration_contract_guard_version = 3
    return video_generator_cls


__all__ = [
    "DEFAULT_COMPLETE_CTA",
    "NarrationContractError",
    "cta_signals",
    "has_complete_cta",
    "sanitize_narration_text",
    "structural_issues",
    "validate_narration_text",
    "install_narration_contract_guard",
]
