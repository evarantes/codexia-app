from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from app.services.cinematic_director import CinematicDirector, CinematicDirectorError, DirectorResult


_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:[-'][0-9A-Za-zÀ-ÖØ-öø-ÿ]+)?", re.UNICODE)


@dataclass
class DurationContractEnforcement:
    plan: Dict[str, Any]
    usage: Dict[str, Any]
    additional_cost_usd: float
    repair_attempts: int


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _word_count(value: Any) -> int:
    return len(_WORD_RE.findall(str(value or "")))


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _merge_usage(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(left or {})
    for key, value in (right or {}).items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = merged[key] + value
        elif key not in merged:
            merged[key] = value
    return merged


def _voice_wpm() -> int:
    try:
        raw = int(float(os.getenv("CODEXIA_DIRECTOR_WPM") or "146"))
    except Exception:
        raw = 146
    return max(110, min(190, raw))


def _duration_tolerance_ratio() -> float:
    try:
        raw = float(os.getenv("CODEXIA_DIRECTOR_DURATION_TOLERANCE") or "0.04")
    except Exception:
        raw = 0.04
    return max(0.02, min(0.12, raw))


def _max_repairs() -> int:
    try:
        raw = int(os.getenv("CODEXIA_DIRECTOR_DURATION_REPAIR_ATTEMPTS") or "2")
    except Exception:
        raw = 2
    return max(0, min(3, raw))


def _scene_narration(plan: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    scenes = [item for item in (plan.get("scenes") or []) if isinstance(item, dict)]
    parts = [_compact(item.get("narration")) for item in scenes]
    parts = [part for part in parts if part]
    return scenes, "\n\n".join(parts).strip()


def _distribute_scene_seconds(scenes: List[Dict[str, Any]], total_seconds: int) -> None:
    if not scenes:
        return
    counts = [max(1, _word_count(scene.get("narration"))) for scene in scenes]
    total_words = max(1, sum(counts))
    minimum = 4
    available = max(0, int(total_seconds) - minimum * len(scenes))
    raw_extra = [(count / total_words) * available for count in counts]
    extras = [int(math.floor(value)) for value in raw_extra]
    remainder = available - sum(extras)
    order = sorted(range(len(scenes)), key=lambda idx: raw_extra[idx] - extras[idx], reverse=True)
    for idx in order[:remainder]:
        extras[idx] += 1
    for idx, scene in enumerate(scenes):
        scene["target_seconds"] = minimum + extras[idx]


def build_duration_contract(
    plan: Dict[str, Any],
    *,
    duration_minutes: int,
    repair_attempts: int = 0,
) -> Dict[str, Any]:
    duration = max(1, int(duration_minutes or 1))
    target_seconds = duration * 60
    wpm = _voice_wpm()
    tolerance = _duration_tolerance_ratio()
    target_words = max(1, int(round(duration * wpm)))
    min_words = max(1, int(math.floor(target_words * (1.0 - tolerance))))
    max_words = max(min_words, int(math.ceil(target_words * (1.0 + tolerance))))

    scenes, canonical_script = _scene_narration(plan)
    scene_words = _word_count(canonical_script)
    full_script = _compact(plan.get("full_script"))
    full_words = _word_count(full_script)
    estimated_seconds = int(round((scene_words / max(1, wpm)) * 60.0))
    full_estimated_seconds = int(round((full_words / max(1, wpm)) * 60.0)) if full_words else 0
    scene_target_seconds = sum(max(0, _safe_int(scene.get("target_seconds"), 0)) for scene in scenes)

    full_script_matches_scenes = bool(
        canonical_script
        and full_script
        and _compact(canonical_script) == full_script
    )
    word_count_valid = min_words <= scene_words <= max_words
    scene_total_valid = bool(scenes) and abs(scene_target_seconds - target_seconds) <= max(8, int(target_seconds * 0.05))
    validated = bool(word_count_valid and canonical_script and scenes and full_script_matches_scenes and scene_total_valid)

    return {
        "version": 1,
        "requested_minutes": duration,
        "target_seconds": target_seconds,
        "voice_wpm": wpm,
        "tolerance_ratio": round(tolerance, 4),
        "target_words": target_words,
        "min_words": min_words,
        "max_words": max_words,
        "actual_words": scene_words,
        "estimated_seconds": estimated_seconds,
        "full_script_words": full_words,
        "full_script_estimated_seconds": full_estimated_seconds,
        "scene_target_seconds": scene_target_seconds,
        "scene_count": len(scenes),
        "full_script_matches_scenes": full_script_matches_scenes,
        "word_count_valid": word_count_valid,
        "scene_timing_valid": scene_total_valid,
        "validated": validated,
        "repair_attempts": int(repair_attempts),
        "locked_after_approval": bool(validated),
    }


def _repair_prompt(
    plan: Dict[str, Any],
    *,
    content_type: str,
    duration_minutes: int,
    contract: Dict[str, Any],
) -> str:
    scenes = [item for item in (plan.get("scenes") or []) if isinstance(item, dict)]
    compact_plan = {
        "content_type": str(content_type or plan.get("content_type") or "story"),
        "theme": plan.get("theme"),
        "title_options": plan.get("title_options"),
        "opening_hook": plan.get("opening_hook"),
        "promise": plan.get("promise"),
        "biblical_accuracy_notes": plan.get("biblical_accuracy_notes"),
        "character_bible": plan.get("character_bible"),
        "application_devotional": plan.get("application_devotional"),
        "closing_prayer_or_reflection": plan.get("closing_prayer_or_reflection"),
        "cta_next_video": plan.get("cta_next_video"),
        "thumbnail_options": plan.get("thumbnail_options"),
        "shorts": plan.get("shorts"),
        "scenes": scenes,
    }
    return f"""
Você é o Diretor-Chefe do Codexia fazendo a checagem FINAL do contrato de produção.
O plano abaixo foi rejeitado pelo relógio de narração. Corrija-o ANTES de qualquer mídia paga.

DURAÇÃO CONTRATADA: {int(duration_minutes)} minutos / {contract['target_seconds']} segundos
RITMO DE CÁLCULO DA VOZ: {contract['voice_wpm']} palavras por minuto
ALVO DE NARRAÇÃO: {contract['target_words']} palavras
FAIXA OBRIGATÓRIA: {contract['min_words']} a {contract['max_words']} palavras
CONTAGEM ATUAL DAS NARRAÇÕES DAS CENAS: {contract['actual_words']} palavras

REGRAS OBRIGATÓRIAS:
1. Preserve tema, fidelidade bíblica, intenção, títulos, ordem narrativa e a quantidade de cenas sempre que possível.
2. Reescreva principalmente o campo "narration" de cada cena para que a SOMA de todas as narrações fique dentro da faixa obrigatória.
3. O campo "full_script" deve ser EXATAMENTE a concatenação, na mesma ordem, dos campos "narration" das cenas, sem acrescentar texto escondido, notas ou outro roteiro paralelo.
4. Distribua "target_seconds" pelas cenas de forma coerente com a quantidade de fala e faça a soma ficar em {contract['target_seconds']} segundos.
5. Não use enchimento artificial, repetições ou frases genéricas apenas para bater tempo. Se precisar expandir, aprofunde a mensagem; se precisar reduzir, corte redundâncias.
6. Preserve visual_prompt, motion_prompt, tier, recommended_provider e generative_video_seconds, exceto se uma pequena adaptação for indispensável para manter coerência com a narração corrigida.
7. Retorne SOMENTE o JSON completo do plano corrigido. Nenhum markdown ou comentário fora do JSON.

PLANO ATUAL:
{json.dumps(compact_plan, ensure_ascii=False)}
""".strip()


def _call_repair(
    director: CinematicDirector,
    provider: str,
    prompt: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    system = (
        director._system_prompt()
        + " Você também é responsável por fechar o contrato exato de duração: um plano fora da faixa de palavras nunca pode ser aprovado."
    )
    provider_name = str(provider or "").strip().lower()
    if provider_name == "anthropic":
        key = director._secret(director.settings, "anthropic_api_key", "ANTHROPIC_API_KEY")
        if not key:
            raise CinematicDirectorError("Claude direto não está disponível para corrigir a duração.")
        raw, usage = director._call_anthropic(key, system, prompt)
    else:
        key = director._secret(director.settings, "openrouter_api_key", "OPENROUTER_API_KEY")
        if not key:
            raise CinematicDirectorError("OpenRouter/Claude não está disponível para corrigir a duração.")
        raw, usage = director._call_openrouter(key, system, prompt)
    return director._extract_json(raw), usage


def enforce_director_duration_contract(
    director: CinematicDirector,
    result: DirectorResult,
    *,
    content_type: str,
    duration_minutes: int,
    budget_brl: float,
) -> DirectorResult:
    """Fail-closed duration contract for Claude-directed productions.

    Claude may draft freely, but the plan is not allowed to leave the director
    stage until the spoken narration fits the requested clock. Repair calls are
    textual only; no image, TTS or video provider is invoked here.
    """
    plan = copy.deepcopy(result.plan if isinstance(result.plan, dict) else {})
    merged_usage = dict(result.usage or {})
    total_cost = float(result.estimated_cost_usd or 0.0)
    repairs = 0

    for attempt in range(_max_repairs() + 1):
        scenes, canonical_script = _scene_narration(plan)
        if scenes:
            _distribute_scene_seconds(scenes, max(1, int(duration_minutes)) * 60)
            plan["scenes"] = scenes
        # The scene narrations are the canonical spoken source. Never let a hidden
        # parallel full_script be longer than what the user reviewed scene by scene.
        if canonical_script:
            plan["full_script"] = canonical_script
        contract = build_duration_contract(plan, duration_minutes=duration_minutes, repair_attempts=repairs)
        plan["duration_contract"] = contract
        checks = plan.get("quality_checks") if isinstance(plan.get("quality_checks"), dict) else {}
        checks["duration_validated"] = bool(contract.get("validated"))
        plan["quality_checks"] = checks
        if contract.get("validated"):
            plan["editorial_reviewed"] = True
            plan["editorial_review_ready"] = True
            plan["narration_locked"] = True
            result.plan = plan
            result.usage = merged_usage
            result.estimated_cost_usd = total_cost
            return result

        if attempt >= _max_repairs():
            break

        prompt = _repair_prompt(
            plan,
            content_type=content_type,
            duration_minutes=duration_minutes,
            contract=contract,
        )
        repaired_raw, repair_usage = _call_repair(director, result.provider, prompt)
        repaired = director._normalize_plan(
            repaired_raw,
            content_type=content_type,
            duration_minutes=duration_minutes,
            budget_brl=budget_brl,
        )
        plan = repaired
        repairs += 1
        merged_usage = _merge_usage(merged_usage, repair_usage)
        total_cost += director._estimate_cost_usd(repair_usage)

    final_contract = build_duration_contract(plan, duration_minutes=duration_minutes, repair_attempts=repairs)
    raise CinematicDirectorError(
        "Claude não conseguiu fechar o contrato de duração antes da produção. "
        f"Alvo {final_contract['target_words']} palavras; faixa {final_contract['min_words']}–{final_contract['max_words']}; "
        f"plano final {final_contract['actual_words']} palavras. Nenhuma mídia paga foi gerada."
    )
