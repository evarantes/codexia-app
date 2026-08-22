from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any, Dict, List, Type

from app.services.recovery_image_budget import resolve_recovery_image_budget


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


_FORBIDDEN_AUTOMATIC_OPENINGS = (
    "uma mensagem de fé",
    "uma mensagem de fe",
    "mensagem de fé para",
    "mensagem de fe para",
    "prepare o coração",
    "prepare o coracao",
    "uma palavra para você",
    "uma palavra para voce",
    "uma reflexão para você",
    "uma reflexao para voce",
)


def _fold(value: Any) -> str:
    text = str(value or "").lower()
    replacements = str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")
    return re.sub(r"\s+", " ", text.translate(replacements)).strip()


def _contains_forbidden_opening(value: Any) -> bool:
    folded = _fold(value)
    return any(_fold(marker) in folded for marker in _FORBIDDEN_AUTOMATIC_OPENINGS)


def prepare_spoken_text(text: Any) -> str:
    """Deterministic pt-BR guard used only immediately before TTS."""
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if not value:
        return value

    # Expressão que já apresentou pronúncia ruim no TTS.
    value = re.sub(r"(?i)(?<!muito\s)\bpelo\s+contrário\b", "muito pelo contrário", value)

    # Forma fonética somente para a voz. Texto/legenda permanecem com a grafia oficial.
    value = re.sub(r"(?i)\bjesus\b", "Jêzus", value)

    # Nunca completa silenciosamente pontes incompletas com outro clichê.
    value = re.sub(
        r"(?i)\b(?:uma|esta)\s+mensagem\s+de\s*(?:\.{2,}|[.!?,;:]|$)",
        "", value,
    )
    value = re.sub(
        r"(?i)\b(?:uma|esta)\s+palavra\s+de\s*(?:\.{2,}|[.!?,;:]|$)",
        "", value,
    )

    value = re.sub(r"\s*([,;:.!?])\s*", r"\1 ", value)
    return re.sub(r"\s+", " ", value).strip()


def _clean_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def premium_endcard_lines(value: Any, max_lines: int = 2, max_chars: int = 38) -> List[str]:
    """Short endcard copy: one reflection, never a paragraph."""
    if isinstance(value, (list, tuple)):
        text = " ".join(_clean_line(item) for item in value if _clean_line(item))
    else:
        text = _clean_line(value)
    if not text:
        text = "Leve esta esperança com você."

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if sentences:
        text = sentences[0]

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
    return (lines or ["Leve esta esperança com você."])[:max_lines]


def _duration_target_seconds(plan: Any) -> int:
    if not isinstance(plan, dict):
        return 0
    for key in ("target_duration_sec", "duration_max_sec", "duration_sec"):
        try:
            value = int(float(plan.get(key) or 0))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return max(30, value)
    for key in ("target_duration_min", "duration_max", "duration_min", "duration"):
        try:
            value = float(plan.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return max(30, int(round(value * 60)))
    return 0


def _narration_word_count(plan: Any) -> int:
    if not isinstance(plan, dict):
        return 0
    parts: List[str] = []
    title = _approved_title_from_plan(plan)
    if title:
        parts.append(title)
    scenes = plan.get("scenes") or []
    if isinstance(scenes, list):
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            for key in ("text", "narration_text", "narration"):
                value = _clean_line(scene.get(key))
                if value:
                    parts.append(value)
                    break
    if not parts:
        for key in ("story_content", "content", "script", "narration_text", "narration"):
            value = _clean_line(plan.get(key))
            if value:
                parts.append(value)
                break
    return len(re.findall(r"\b[\wÀ-ÿ]+\b", " ".join(parts), flags=re.UNICODE))


def _duration_preflight(plan: Any) -> Dict[str, Any]:
    target_sec = _duration_target_seconds(plan)
    word_count = _narration_word_count(plan)
    if target_sec <= 0 or word_count <= 0:
        return {
            "checked": False,
            "passed": True,
            "reason": "duration_target_or_narration_missing",
            "target_sec": target_sec,
            "word_count": word_count,
        }

    try:
        wpm = int(float(os.getenv("VIDEO_DURATION_ESTIMATED_WPM") or "150"))
    except (TypeError, ValueError):
        wpm = 150
    wpm = max(120, min(190, wpm))

    try:
        tolerance_pct = float(os.getenv("VIDEO_DURATION_TARGET_TOLERANCE_PCT") or "25")
    except (TypeError, ValueError):
        tolerance_pct = 25.0
    tolerance_pct = max(10.0, min(60.0, tolerance_pct))

    try:
        extra_seconds = int(float(os.getenv("VIDEO_DURATION_TARGET_EXTRA_SECONDS") or "20"))
    except (TypeError, ValueError):
        extra_seconds = 20
    extra_seconds = max(5, min(60, extra_seconds))

    estimated_sec = max(1, int(round((word_count / float(wpm)) * 60.0)))
    allowed_extra = max(extra_seconds, int(round(target_sec * (tolerance_pct / 100.0))))
    max_sec = target_sec + allowed_extra
    passed = estimated_sec <= max_sec
    return {
        "checked": True,
        "passed": passed,
        "target_sec": target_sec,
        "estimated_sec": estimated_sec,
        "max_sec": max_sec,
        "word_count": word_count,
        "estimated_wpm": wpm,
        "tolerance_pct": tolerance_pct,
        "extra_seconds": extra_seconds,
        "reason": "within_editorial_tolerance" if passed else "estimated_duration_far_above_target",
    }


def apply_channel_excellence_rollout() -> Dict[str, Any]:
    defaults = {
        "ENABLE_CHANNEL_EXCELLENCE_GUARD": "true",
        "ENABLE_APPROVED_NARRATION_ONLY": "true",
        "ENABLE_STRICT_VISUAL_UNIQUENESS": "true",
        "ENABLE_FINAL_VIDEO_QUALITY_GATE": "true",
        "ENABLE_DURATION_SANITY_PREFLIGHT": "true",
    }
    for name, value in defaults.items():
        if name not in os.environ:
            os.environ[name] = value
    return {
        "enabled": _enabled("ENABLE_CHANNEL_EXCELLENCE_GUARD", "true"),
        "tts_pronunciation_guard": True,
        "premium_endcard": True,
        "approved_narration_only": _enabled("ENABLE_APPROVED_NARRATION_ONLY", "true"),
        "strict_visual_uniqueness": _enabled("ENABLE_STRICT_VISUAL_UNIQUENESS", "true"),
        "final_quality_gate": _enabled("ENABLE_FINAL_VIDEO_QUALITY_GATE", "true"),
        "duration_sanity_preflight": _enabled("ENABLE_DURATION_SANITY_PREFLIGHT", "true"),
    }


def _approved_title_from_plan(plan: Any) -> str:
    if not isinstance(plan, dict):
        return ""
    for key in ("override_title", "title", "topic"):
        value = _clean_line(plan.get(key))
        if value:
            return value[:180]
    return ""


def _approved_opening_for_tts(plan: Any) -> str:
    """Preserve the approved title verbatim while making it a closed utterance.

    Approved titles are intentionally reused as the spoken opening. A title is
    allowed to omit terminal punctuation in the UI, but the pre-TTS narration
    contract requires every utterance to be explicitly closed. Only punctuation
    is added here; wording is never generated or inferred.
    """
    value = _approved_title_from_plan(plan)
    if not value or re.search(r"[.!?…][\"”’')\]]*$", value):
        return value
    return value.rstrip(" ,;:-—–") + "."


def _has_manual_visuals(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    selected = plan.get("selected_images") or plan.get("custom_image_paths") or []
    if isinstance(selected, (list, tuple)) and any(str(item or "").strip() for item in selected):
        return True
    scenes = plan.get("scenes") or []
    for scene in scenes if isinstance(scenes, list) else []:
        if not isinstance(scene, dict):
            continue
        if any(str(scene.get(key) or "").strip() for key in ("image_path", "selected_image", "image")):
            return True
    return False


def _recovery_visual_budget(plan: Any) -> Dict[str, Any]:
    budget = resolve_recovery_image_budget(plan)
    return budget if isinstance(budget, dict) else {"enabled": False}


def _quality_gate(result: Any, plan: Any = None) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "passed": True,
        "checks": {},
        "violations": [],
        "manual_visuals": _has_manual_visuals(plan),
        "budget_limited_visuals": bool(_recovery_visual_budget(plan).get("enabled")),
    }
    if not isinstance(result, dict):
        report["passed"] = False
        report["violations"].append("render_result_missing")
        return report

    rr = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
    narration = rr.get("narration_plan") if isinstance(rr.get("narration_plan"), dict) else {}
    visual = rr.get("visual_plan") if isinstance(rr.get("visual_plan"), dict) else {}

    opening = str(narration.get("opening_text") or "").strip()
    no_generic_opening = not _contains_forbidden_opening(opening)
    report["checks"]["no_generic_automatic_opening"] = no_generic_opening
    if not no_generic_opening:
        report["violations"].append("generic_automatic_opening")

    closing = str(narration.get("closing_text") or "").strip()
    no_hidden_spoken_cta = not bool(closing)
    report["checks"]["no_hidden_spoken_cta"] = no_hidden_spoken_cta
    if not no_hidden_spoken_cta:
        report["violations"].append("hidden_spoken_closing")

    # final_visual_quality_gate_self_heal_v1
    generated = int(visual.get("generated_image_count") or 0)
    reused = int(visual.get("reused_image_count") or 0)
    legacy_avg_hold = float(visual.get("average_image_duration_sec") or 0.0)
    manual_visuals = bool(report["manual_visuals"])
    budget_limited_visuals = bool(report["budget_limited_visuals"])

    # O renderer já divide cenas longas em beats cinematográficos. O antigo
    # cálculo somava a duração total por CAMINHO de imagem e produzia falso
    # positivo quando um arquivo era reutilizado. A qualidade visual deve medir
    # o maior hold real de cada beat, não a duração acumulada do asset.
    scene_visuals = rr.get("scene_visuals") if isinstance(rr.get("scene_visuals"), list) else []
    beat_holds = []
    for item in scene_visuals:
        if not isinstance(item, dict):
            continue
        try:
            hold = float(item.get("max_visual_hold_sec") or 0.0)
        except (TypeError, ValueError):
            hold = 0.0
        if hold > 0:
            beat_holds.append(hold)

    resource_profile = rr.get("resource_profile") if isinstance(rr.get("resource_profile"), dict) else {}
    try:
        planned_hold_target = float(resource_profile.get("visual_hold_target_sec") or 0.0)
    except (TypeError, ValueError):
        planned_hold_target = 0.0
    hold_limit = max(11.0, planned_hold_target + 0.75 if planned_hold_target > 0 else 11.0)
    max_beat_hold = max(beat_holds) if beat_holds else 0.0
    avg_beat_hold = (sum(beat_holds) / len(beat_holds)) if beat_holds else 0.0

    if generated > 1 and not manual_visuals and not budget_limited_visuals:
        no_path_reuse = reused == 0
        pacing_ok = max_beat_hold <= hold_limit if max_beat_hold > 0 else True
    else:
        no_path_reuse = True
        pacing_ok = True

    report["checks"]["no_reused_generated_image_paths"] = no_path_reuse
    report["checks"]["visual_beat_hold_ok"] = pacing_ok
    report["metrics"] = {
        "generated_image_count": generated,
        "reused_image_count": reused,
        "legacy_average_image_duration_sec": round(legacy_avg_hold, 2),
        "average_visual_beat_hold_sec": round(avg_beat_hold, 2),
        "max_visual_beat_hold_sec": round(max_beat_hold, 2),
        "visual_hold_limit_sec": round(hold_limit, 2),
        "visual_hold_target_sec": round(planned_hold_target, 2),
    }
    if budget_limited_visuals:
        budget = _recovery_visual_budget(plan)
        visual_budget = visual.get("recovery_image_budget") if isinstance(visual.get("recovery_image_budget"), dict) else {}
        allowed = int(budget.get("allowed_new_image_calls") or 0)
        used = int(visual_budget.get("used_new_image_calls") or 0)
        respected = used <= allowed
        report["checks"]["confirmed_recovery_image_budget_respected"] = respected
        report["metrics"]["confirmed_new_image_limit"] = allowed
        report["metrics"]["paid_image_calls_used"] = used
        report["metrics"]["confirmed_max_image_cost_usd"] = float(
            visual_budget.get("confirmed_max_image_cost_usd") or budget.get("confirmed_max_image_cost_usd") or 0.0
        )
        report["metrics"]["confirmed_max_image_cost_brl"] = float(
            visual_budget.get("confirmed_max_image_cost_brl") or budget.get("confirmed_max_image_cost_brl") or 0.0
        )
        report["metrics"]["estimated_consumed_image_cost_usd"] = float(
            visual_budget.get("estimated_consumed_image_cost_usd") or 0.0
        )
        report["metrics"]["estimated_consumed_image_cost_brl"] = float(
            visual_budget.get("estimated_consumed_image_cost_brl") or 0.0
        )
        if not respected:
            report["violations"].append("confirmed_recovery_image_budget_exceeded")

    # Esses dois sinais são importantes para a REVISÃO HUMANA, mas não podem
    # destruir um MP4 válido já renderizado nem forçar novo gasto automático.
    # O renderer aplica movimentos/beats diferentes quando há reaproveitamento.
    visual_warnings = []
    if not no_path_reuse:
        visual_warnings.append("generated_image_path_reused")
    if not pacing_ok:
        visual_warnings.append("visual_hold_too_long")

    # Violações anteriores continuam bloqueantes. Os alertas visuais são
    # mantidos para revisão humana sem invalidar um render já concluído.
    blocking_violations = list(report["violations"])
    report["violations"].extend(visual_warnings)
    report["warnings"] = visual_warnings
    report["blocking_violations"] = blocking_violations
    report["review_recommended"] = bool(visual_warnings)
    report["auto_render_preserved"] = bool(visual_warnings)
    report["passed"] = not blocking_violations
    return report


def install_channel_excellence_guard_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    if getattr(video_generator_cls, "_codexia_channel_excellence_guard_installed", False):
        return video_generator_cls

    original_audio = getattr(video_generator_cls, "generate_audio", None)
    if callable(original_audio):
        def guarded_audio(self: Any, text: Any, *args: Any, **kwargs: Any):
            if not _enabled("ENABLE_CHANNEL_EXCELLENCE_GUARD", "true"):
                return original_audio(self, text, *args, **kwargs)
            mutable_args = list(args)
            if mutable_args and isinstance(mutable_args[0], str) and mutable_args[0].strip().lower().startswith("pt"):
                mutable_args[0] = "pt"
            if "lang" in kwargs and str(kwargs.get("lang") or "").strip().lower().startswith("pt"):
                kwargs["lang"] = "pt"
            return original_audio(self, prepare_spoken_text(text), *mutable_args, **kwargs)
        video_generator_cls.generate_audio = guarded_audio

    # Abertura determinística: somente o título aprovado. Nada é inventado pelo renderer.
    original_opening = getattr(video_generator_cls, "_default_opening_text", None)
    if callable(original_opening):
        def approved_opening(self: Any, channel_name: str, *, plan=None):
            if not _enabled("ENABLE_APPROVED_NARRATION_ONLY", "true"):
                return original_opening(self, channel_name, plan=plan)
            return _approved_opening_for_tts(plan)
        video_generator_cls._default_opening_text = approved_opening

    # Não injeta reflexão escondida além do roteiro que o usuário aprovou.
    original_reflection = getattr(video_generator_cls, "_default_reflection_text", None)
    if callable(original_reflection):
        def no_hidden_reflection(self: Any, plan=None, scenes=None):
            if not _enabled("ENABLE_APPROVED_NARRATION_ONLY", "true"):
                return original_reflection(self, plan, scenes)
            return ""
        video_generator_cls._default_reflection_text = no_hidden_reflection

    # CTA é visual no endcard; não é mais acrescentado silenciosamente à locução.
    original_default_closing = getattr(video_generator_cls, "_default_closing_text", None)
    if callable(original_default_closing):
        def no_hidden_closing(self: Any, channel_name: str):
            if not _enabled("ENABLE_APPROVED_NARRATION_ONLY", "true"):
                return original_default_closing(self, channel_name)
            return ""
        video_generator_cls._default_closing_text = no_hidden_closing

    # O planner não pode reduzir várias cenas automáticas a poucas imagens compartilhadas.
    original_target_visual_count = getattr(video_generator_cls, "_target_visual_count", None)
    if callable(original_target_visual_count):
        def one_visual_per_scene_target(self: Any, scenes: Any, plan=None, *args: Any, **kwargs: Any):
            if (
                not _enabled("ENABLE_STRICT_VISUAL_UNIQUENESS", "true")
                or _has_manual_visuals(plan)
                or bool(_recovery_visual_budget(plan).get("enabled"))
            ):
                if plan is None:
                    return original_target_visual_count(self, scenes, *args, **kwargs)
                return original_target_visual_count(self, scenes, plan, *args, **kwargs)
            return max(1, len(list(scenes or [])))
        video_generator_cls._target_visual_count = one_visual_per_scene_target

    original_transition = getattr(video_generator_cls, "_build_visual_transition_decision", None)
    if callable(original_transition):
        def force_new_visual_transition(self: Any, previous_profile: Dict[str, Any], current_profile: Dict[str, Any]):
            decision = original_transition(self, previous_profile, current_profile)
            if not _enabled("ENABLE_STRICT_VISUAL_UNIQUENESS", "true"):
                return decision
            payload = dict(decision or {})
            payload["should_generate_new"] = True
            payload["forced_by_channel_excellence"] = True
            return payload
        video_generator_cls._build_visual_transition_decision = force_new_visual_transition

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

            duration_preflight = _duration_preflight(guarded)
            if (
                _enabled("ENABLE_DURATION_SANITY_PREFLIGHT", "true")
                and duration_preflight.get("checked")
                and not duration_preflight.get("passed")
            ):
                raise RuntimeError(
                    "Roteiro fora da tolerância editorial de duração antes de gerar mídia paga: "
                    f"alvo {int(duration_preflight.get('target_sec') or 0)}s, "
                    f"estimado {int(duration_preflight.get('estimated_sec') or 0)}s, "
                    f"limite flexível {int(duration_preflight.get('max_sec') or 0)}s. "
                    "Revise ou condense o roteiro sem cortar o fechamento natural."
                )

            closing_source = guarded.get("final_message") or guarded.get("closing_message")
            lines = premium_endcard_lines(closing_source)
            guarded["final_message"] = lines
            guarded["closing_message"] = " ".join(lines)
            guarded["endcard_cta_text"] = "Inscreva-se e acompanhe novas mensagens."
            branding = guarded.get("branding") if isinstance(guarded.get("branding"), dict) else {}
            branding = deepcopy(branding)
            branding["final_message"] = lines
            branding.setdefault("endcard_cta_text", guarded["endcard_cta_text"])
            branding.setdefault("aspect_ratio", guarded.get("aspect_ratio") or "16:9")
            guarded["branding"] = branding

            result = original_create(self, guarded, *args, **kwargs)
            if isinstance(result, dict):
                quality = _quality_gate(result, plan=guarded)
                result["channel_excellence_guard"] = {
                    "enabled": True,
                    "endcard_lines": lines,
                    "endcard_cta_text": guarded["endcard_cta_text"],
                    "duration_preflight": deepcopy(duration_preflight),
                    "quality_gate": quality,
                }
                rr = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
                rr["duration_preflight"] = deepcopy(duration_preflight)
                rr["final_video_quality_gate"] = deepcopy(quality)
                result["render_report"] = rr
                if _enabled("ENABLE_FINAL_VIDEO_QUALITY_GATE", "true") and not quality.get("passed"):
                    blocking = quality.get("blocking_violations") or quality.get("violations") or []
                    violations = ", ".join(str(item) for item in blocking)
                    raise RuntimeError(
                        "Vídeo reprovado pelo controle final de qualidade antes da revisão: "
                        + (violations or "qualidade insuficiente")
                    )
            return result
        video_generator_cls.create_video_from_plan = create_with_excellence_guard

    video_generator_cls._codexia_channel_excellence_guard_installed = True
    return video_generator_cls
