from __future__ import annotations

import html
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Type

from app.services.narration_core import (
    NARRATION_CORE_NAMESPACE,
    NARRATION_CORE_VERSION,
    NarrationCoreError,
    build_narration_artifact,
)
from app.services.video_creation_standard import (
    STANDARD_COMPLETE_CTA,
    STANDARD_REQUIRED_CTA_SIGNALS,
    apply_standard_video_structure,
)


DEFAULT_COMPLETE_CTA = STANDARD_COMPLETE_CTA

_SAFE_SSML_CONTAINER_TAG = re.compile(
    r"(?is)</?\s*(?:speak|prosody|voice|p|s|emphasis|sub|phoneme)\b[^>]*>"
)
_BREAK_TAG = re.compile(r"(?is)<\s*break\b[^>]*?/?>")
_STRUCTURAL_PATTERNS = (
    ("code_fence", re.compile(r"```|~~~")),
    ("json_field", re.compile(
        r"(?i)[\"']?(?:image_prompt|visual_prompt|camera_movement|motion_effect|scene_qc|"
        r"scene_card|narration_text|on_screen_text|selected_images|generated_image_path|"
        r"asset_path|render_report|prompt|metadata|payload)[\"']?\s*:"
    )),
    ("json_object", re.compile(r"\{\s*[\"'][A-Za-z_][A-Za-z0-9_]*[\"']\s*:")),
    ("technical_label", re.compile(
        r"(?im)^\s*(?:cena\s*\d+|scene\s*\d+|prompt\s*visual|prompt\s*de\s*imagem|"
        r"image_prompt|visual_prompt|dura[cç][aã]o|duration|movimento\s*(?:de\s*)?c[aâ]mera|"
        r"camera_movement|texto\s*na\s*tela|on_screen_text|metadata|payload|render_report)\s*[:=\-–—]?"
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


def sanitize_narration_text(text: Any) -> str:
    """Limpeza conservadora para texto que já deveria ser fala pura."""
    raw = unicodedata.normalize("NFKC", html.unescape(str(text or ""))).replace("\x00", " ")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = _BREAK_TAG.sub(", ", raw)
    raw = _SAFE_SSML_CONTAINER_TAG.sub(" ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" ,")
    return raw


def structural_issues(text: Any) -> List[str]:
    raw = str(text or "")
    issues = [name for name, pattern in _STRUCTURAL_PATTERNS if pattern.search(raw)]
    if re.search(r"\\[nrt]\s*[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?\s*:", raw):
        issues.append("escaped_serialized_field")
    technical_field_pattern = re.compile(
        r"(?i)(?<!\w)(?:status|progress|output_path|file_path|video_path|audio_path|"
        r"task_id|job_id|request_id|executor_id|provider|model|pipeline_stage|"
        r"render_stage|error_code|result_json|payload_json)\s*[:=]"
    )
    if len(technical_field_pattern.findall(raw)) >= 2:
        issues.append("serialized_technical_payload")
    return sorted(set(issues))


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


def validate_narration_text(
    text: Any,
    *,
    label: str = "narração",
    require_terminal_sentence: bool = True,
    require_complete_cta: bool = False,
) -> str:
    """Validador estrito para texto que já atravessou a fronteira falável.

    Este método não extrai JSON ou roteiro técnico. Para entradas mistas use
    ``prepare_spoken_narration_text``; assim não existe limpeza silenciosa em
    lugares que deveriam receber apenas fala pura.
    """
    cleaned = sanitize_narration_text(text)
    if not cleaned:
        raise NarrationContractError(f"{label}: texto vazio; TTS bloqueado.")
    issues = structural_issues(cleaned)
    if issues:
        raise NarrationContractError(
            f"{label}: conteúdo técnico detectado ({', '.join(issues)}); TTS bloqueado."
        )
    if require_terminal_sentence and not re.search(r"[.!?…][\"”’')\]]*$", cleaned):
        raise NarrationContractError(f"{label}: frase final parece truncada; TTS bloqueado.")
    if require_complete_cta and not has_complete_cta(cleaned):
        missing = sorted(STANDARD_REQUIRED_CTA_SIGNALS - cta_signals(cleaned))
        raise NarrationContractError(
            f"{label}: CTA incompleto ({', '.join(missing)}); TTS bloqueado."
        )
    return cleaned


def prepare_spoken_narration_text(text: Any, *, label: str = "narração") -> str:
    """Única fronteira aceita para conteúdo potencialmente misto antes do TTS."""
    try:
        artifact = build_narration_artifact(text)
    except NarrationCoreError as exc:
        raise NarrationContractError(f"{label}: {exc}; TTS bloqueado.") from exc
    return validate_narration_text(artifact.spoken_text, label=label)


def _scene_text(scene: Dict[str, Any]) -> str:
    for key in ("_tts_text", "text", "narration_text", "narration", "content"):
        value = scene.get(key)
        if value:
            try:
                return prepare_spoken_narration_text(value, label="cena")
            except NarrationContractError:
                continue
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
        value = plan.get(key)
        if value:
            try:
                candidates.append(prepare_spoken_narration_text(value, label="CTA"))
            except NarrationContractError:
                pass
    if fallback:
        try:
            candidates.append(prepare_spoken_narration_text(fallback, label="CTA"))
        except NarrationContractError:
            pass
    candidates.append(DEFAULT_COMPLETE_CTA)
    return next((value for value in candidates if has_complete_cta(value)), DEFAULT_COMPLETE_CTA)


def install_narration_contract_guard(video_generator_cls: Type[Any]) -> Type[Any]:
    """Instala uma única fronteira Narration Core v1 nas portas reais de TTS."""
    if getattr(video_generator_cls, "_codexia_narration_core_v1", False):
        return video_generator_cls

    original_prepare = getattr(video_generator_cls, "prepare_final_narration_text", None)
    original_generate_audio = getattr(video_generator_cls, "generate_audio", None)
    original_segmented = getattr(video_generator_cls, "_compose_segmented_narration_audio", None)

    if callable(original_prepare):
        def prepare_guarded(self: Any, plan: Optional[Dict[str, Any]], scenes: List[Dict[str, Any]], voice_style=None, voice_gender=None):
            if isinstance(plan, dict):
                apply_standard_video_structure(plan)
            safe_plan = dict(plan or {}) if isinstance(plan, dict) else {}

            # Áudio supervisionado é uma fonte fechada: se foi exigido, nenhum TTS novo
            # pode acontecer. A ausência do MP3 para imediatamente antes de providers.
            approved_required = bool(safe_plan.get("approved_narration_required"))
            seed_audio_path = str(safe_plan.get("seed_audio_path") or "").strip()
            seed_valid = bool(seed_audio_path and os.path.isfile(seed_audio_path) and os.path.getsize(seed_audio_path) > 512)
            self._codexia_approved_narration_required = approved_required
            self._codexia_approved_seed_audio_path = seed_audio_path if seed_valid else ""
            if approved_required and not seed_valid:
                raise NarrationContractError(
                    "Narração aprovada obrigatória, mas o MP3 aprovado não está disponível; "
                    "render e novo TTS foram bloqueados."
                )

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
            opening = prepare_spoken_narration_text(meta.get("opening_text"), label="abertura")
            body = prepare_spoken_narration_text(meta.get("body_text"), label="corpo da narração")
            raw_reflection = meta.get("reflection_text")
            reflection = prepare_spoken_narration_text(raw_reflection, label="reflexão final") if raw_reflection else ""

            if reflection and body.endswith(reflection):
                body = body[:-len(reflection)].rstrip()
                if body and not re.search(r"[.!?…][\"”’')\]]*$", body):
                    body = body.rstrip(",;:-") + "."
                body = validate_narration_text(body, label="corpo da narração")

            cta = _pick_complete_cta(safe_plan, marked_cta, meta.get("cta_text") or meta.get("closing_text"))
            cta = validate_narration_text(cta, label="CTA final", require_complete_cta=True)
            full_text = " ".join(part for part in [opening, body, reflection, cta] if part).strip()
            full_text = validate_narration_text(full_text, label="narração final")

            meta.update({
                "opening_text": opening,
                "body_text": body,
                "reflection_text": reflection,
                "cta_text": cta,
                "closing_text": cta,
                "full_text": full_text,
                "narration_core_version": NARRATION_CORE_VERSION,
                "narration_core_namespace": NARRATION_CORE_NAMESPACE,
                "approved_narration_required": approved_required,
                "approved_narration_seed_valid": seed_valid,
                "video_creation_standard": dict(safe_plan.get("codexia_video_standard") or {}),
            })
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
            meta["protected_closing_contract"] = {
                "version": NARRATION_CORE_VERSION,
                "reflection_protected": bool(reflection),
                "cta_protected": True,
                "cta_signals": sorted(cta_signals(cta)),
                "required_cta_signals": sorted(STANDARD_REQUIRED_CTA_SIGNALS),
                "narration_core_namespace": NARRATION_CORE_NAMESPACE,
                "tts_plain_text_only": True,
                "tts_allowed": not approved_required,
            }
            return meta

        video_generator_cls.prepare_final_narration_text = prepare_guarded

    if callable(original_segmented):
        def segmented_guarded(self: Any, *, main_text: str, cta_text: str, **kwargs: Any):
            if bool(getattr(self, "_codexia_approved_narration_required", False)):
                raise NarrationContractError(
                    "Áudio aprovado obrigatório: síntese segmentada bloqueada para preservar o MP3 aprovado."
                )
            main_clean = prepare_spoken_narration_text(main_text, label="narração principal")
            try:
                cta_clean = prepare_spoken_narration_text(cta_text, label="CTA final")
            except NarrationContractError:
                cta_clean = DEFAULT_COMPLETE_CTA
            if not has_complete_cta(cta_clean):
                cta_clean = DEFAULT_COMPLETE_CTA
            cta_clean = validate_narration_text(cta_clean, label="CTA final", require_complete_cta=True)
            return original_segmented(self, main_text=main_clean, cta_text=cta_clean, **kwargs)
        video_generator_cls._compose_segmented_narration_audio = segmented_guarded

    if callable(original_generate_audio):
        def generate_audio_guarded(self: Any, text: str, *args: Any, **kwargs: Any):
            if bool(getattr(self, "_codexia_approved_narration_required", False)):
                raise NarrationContractError(
                    "Áudio aprovado obrigatório: novo TTS bloqueado. O renderer deve reutilizar o MP3 aprovado."
                )
            clean = prepare_spoken_narration_text(text, label="texto enviado ao TTS")
            try:
                debug = dict(getattr(self, "_last_tts_debug", {}) or {})
                debug.update({
                    "narration_core_version": NARRATION_CORE_VERSION,
                    "narration_core_namespace": NARRATION_CORE_NAMESPACE,
                    "tts_plain_text_only": True,
                    "remaining_structural_issues": structural_issues(clean),
                })
                self._last_tts_debug = debug
            except Exception:
                pass
            output = original_generate_audio(self, clean, *args, **kwargs)
            task_id = str(getattr(self, "_codexia_task_id", "") or "").strip()
            if task_id and output and isinstance(output, str) and os.path.isfile(output):
                try:
                    from app.services.production_manifest import record_artifact
                    record_artifact(task_id, output, kind="audio", source="tts_narration_core_v1")
                except Exception:
                    pass
            return output
        video_generator_cls.generate_audio = generate_audio_guarded

    video_generator_cls._codexia_narration_core_v1 = True
    # Compatibilidade externa: consumidores antigos só verificam presença do guard.
    video_generator_cls._codexia_narration_contract_guard_v1 = True
    video_generator_cls._codexia_narration_contract_guard_version = NARRATION_CORE_VERSION
    return video_generator_cls


__all__ = [
    "DEFAULT_COMPLETE_CTA",
    "NarrationContractError",
    "cta_signals",
    "has_complete_cta",
    "sanitize_narration_text",
    "structural_issues",
    "validate_narration_text",
    "prepare_spoken_narration_text",
    "install_narration_contract_guard",
]
