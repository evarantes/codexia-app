#!/usr/bin/env python3
"""Permite vídeos narrados com duração em segundos.

Este hardening roda imediatamente depois de
``apply_duration_confirmation_hardening.py``. O contrato legado continua usando
minutos para compatibilidade, porém aceita minutos fracionários de ponta a ponta
(15 s = 0,25 min). A UI apresenta segundos diretamente para que testes de
narração, imagens, legenda e render possam ser executados muito mais rápido.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
MARKER = "CODEXIA_DURATION_SECONDS_SUPPORT_V1"


class PatchError(RuntimeError):
    pass


def _once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def _all(text: str, old: str, new: str, expected: int, label: str) -> str:
    count = text.count(old)
    if count != expected:
        raise PatchError(f"{label}: esperado {expected} trecho(s), encontrado {count}")
    return text.replace(old, new)


def patch_index(text: str) -> str:
    if MARKER in text:
        return text

    text = _once(
        text,
        '''                            <div>
                                <label class="block font-bold mb-2">Duração mín (min)</label>
                                <input v-model="ytStoryDurationMin" type="number" min="1" max="60" class="w-full border p-2 rounded bg-white">
                            </div>
                            <div>
                                <label class="block font-bold mb-2">Duração máx (min)</label>
                                <input v-model="ytStoryDurationMax" type="number" min="1" max="60" class="w-full border p-2 rounded bg-white">
                            </div>''',
        '''                            <div>
                                <label class="block font-bold mb-2">Unidade da duração</label>
                                <select v-model="ytStoryDurationUnit" class="w-full border p-2 rounded bg-white">
                                    <option value="seconds">Segundos — teste rápido</option>
                                    <option value="minutes">Minutos — produção normal</option>
                                </select>
                            </div>
                            <div>
                                <label class="block font-bold mb-2">Duração mín ({{ ytStoryDurationUnit === 'seconds' ? 's' : 'min' }})</label>
                                <input v-model.number="ytStoryDurationMin" type="number"
                                    :min="ytStoryDurationUnit === 'seconds' ? 5 : 1"
                                    :max="ytStoryDurationUnit === 'seconds' ? 3600 : 60"
                                    :step="ytStoryDurationUnit === 'seconds' ? 1 : 0.5"
                                    class="w-full border p-2 rounded bg-white">
                            </div>
                            <div>
                                <label class="block font-bold mb-2">Duração máx ({{ ytStoryDurationUnit === 'seconds' ? 's' : 'min' }})</label>
                                <input v-model.number="ytStoryDurationMax" type="number"
                                    :min="ytStoryDurationUnit === 'seconds' ? 5 : 1"
                                    :max="ytStoryDurationUnit === 'seconds' ? 3600 : 60"
                                    :step="ytStoryDurationUnit === 'seconds' ? 1 : 0.5"
                                    class="w-full border p-2 rounded bg-white">
                                <div v-if="ytStoryDurationUnit === 'seconds'" class="text-xs text-emerald-700 mt-1">
                                    Teste rápido: 10–30 s é ideal para validar narração, imagens, legenda e render.
                                </div>
                            </div>''',
        "index/controls",
    )

    text = _once(
        text,
        '''                    ytStoryImproveInstruction: '',
                    ytStoryDurationMin: 10,
                    ytStoryDurationMax: 15,''',
        '''                    ytStoryImproveInstruction: '',
                    ytStoryDurationUnit: 'minutes',
                    ytStoryDurationMin: 10,
                    ytStoryDurationMax: 15,''',
        "index/state",
    )

    text = _once(
        text,
        '''                    const durationRaw = Number(source.duration || 5);
                    const duration = Number.isFinite(durationRaw) ? Math.max(1, Math.min(60, Math.round(durationRaw))) : 5;''',
        '''                    const durationRaw = Number(source.duration || 5);
                    const duration = Number.isFinite(durationRaw)
                        ? Math.max(5 / 60, Math.min(60, Math.round(durationRaw * 60) / 60))
                        : 5;''',
        "index/normalizer",
    )

    text = _once(
        text,
        '''                    if (!narrationText) {
                        const min = Number(this.ytStoryDurationMin || 0);
                        const max = Number(this.ytStoryDurationMax || 0);
                        const fallbackMinutes = Math.max(1, Math.round(((min || 0) + (max || min || 1)) / (max ? 2 : 1)) || 1);
                        return fallbackMinutes * 60;
                    }''',
        '''                    if (!narrationText) {
                        const unit = this.ytStoryDurationUnit === 'seconds' ? 'seconds' : 'minutes';
                        const min = Number(this.ytStoryDurationMin || 0);
                        const max = Number(this.ytStoryDurationMax || 0);
                        const fallback = ((min || 0) + (max || min || (unit === 'seconds' ? 15 : 1))) / (max ? 2 : 1);
                        return unit === 'seconds'
                            ? Math.max(5, Math.round(fallback || 15))
                            : Math.max(60, Math.round((fallback || 1) * 60));
                    }''',
        "index/prediction-fallback",
    )

    vars_old = '''                        const durationMin = Number(this.ytStoryDurationMin || 10);
                        const durationMax = Number(this.ytStoryDurationMax || durationMin);'''
    vars_new = '''                        const durationUnit = this.ytStoryDurationUnit === 'seconds' ? 'seconds' : 'minutes';
                        const rawDurationMin = Number(this.ytStoryDurationMin || (durationUnit === 'seconds' ? 15 : 10));
                        const rawDurationMax = Number(this.ytStoryDurationMax || rawDurationMin);
                        const durationMinValue = durationUnit === 'seconds'
                            ? Math.max(5, Math.min(3600, rawDurationMin || 15))
                            : Math.max(1, Math.min(60, rawDurationMin || 10));
                        const durationMaxValue = durationUnit === 'seconds'
                            ? Math.max(durationMinValue, Math.min(3600, rawDurationMax || durationMinValue))
                            : Math.max(durationMinValue, Math.min(60, rawDurationMax || durationMinValue));
                        const durationMin = durationUnit === 'seconds' ? durationMinValue / 60 : durationMinValue;
                        const durationMax = durationUnit === 'seconds' ? durationMaxValue / 60 : durationMaxValue;'''
    text = _all(text, vars_old, vars_new, 2, "index/text-duration-vars")

    text = _all(
        text,
        '''                                duration_min: durationMin,
                                duration_max: durationMax''',
        '''                                duration_min: durationMin,
                                duration_max: durationMax,
                                duration_unit: durationUnit''',
        2,
        "index/text-duration-payload",
    )

    text = _once(
        text,
        '''                        const requestedMin = Math.max(1, Math.min(60, Number(this.ytStoryDurationMin || 1) || 1));
                        const requestedMax = Math.max(requestedMin, Math.min(60, Number(this.ytStoryDurationMax || requestedMin) || requestedMin));
                        const duration = requestedMax;
                        const predictedSeconds = Math.max(0, Number(this.ytStoryPredictedDurationSeconds || 0) || 0);
                        const requestedMinSeconds = requestedMin * 60;
                        const requestedMaxSeconds = requestedMax * 60;
                        let durationOverrideApproved = false;

                        if (predictedSeconds > 0 && (predictedSeconds < requestedMinSeconds || predictedSeconds > requestedMaxSeconds)) {
                            const isOver = predictedSeconds > requestedMaxSeconds;
                            const referenceSeconds = isOver ? requestedMaxSeconds : requestedMinSeconds;
                            const deltaSeconds = Math.max(1, Math.round(Math.abs(predictedSeconds - referenceSeconds)));
                            const requestedLabel = requestedMin === requestedMax
                                ? `${requestedMin} min`
                                : `${requestedMin} a ${requestedMax} min`;
                            const predictedLabel = this.formatNarrationDurationHuman(predictedSeconds);
                            const deltaLabel = this.formatNarrationDurationHuman(deltaSeconds);
                            const directionLabel = isOver ? 'acima' : 'abaixo';''',
        '''                        const durationUnit = this.ytStoryDurationUnit === 'seconds' ? 'seconds' : 'minutes';
                        const rawRequestedMin = Number(this.ytStoryDurationMin || (durationUnit === 'seconds' ? 15 : 1));
                        const rawRequestedMax = Number(this.ytStoryDurationMax || rawRequestedMin);
                        const requestedMinValue = durationUnit === 'seconds'
                            ? Math.max(5, Math.min(3600, rawRequestedMin || 15))
                            : Math.max(1, Math.min(60, rawRequestedMin || 1));
                        const requestedMaxValue = durationUnit === 'seconds'
                            ? Math.max(requestedMinValue, Math.min(3600, rawRequestedMax || requestedMinValue))
                            : Math.max(requestedMinValue, Math.min(60, rawRequestedMax || requestedMinValue));
                        const requestedMinSeconds = durationUnit === 'seconds' ? requestedMinValue : requestedMinValue * 60;
                        const requestedMaxSeconds = durationUnit === 'seconds' ? requestedMaxValue : requestedMaxValue * 60;
                        const requestedMin = requestedMinSeconds / 60;
                        const requestedMax = requestedMaxSeconds / 60;
                        const duration = requestedMax;
                        const predictedSeconds = Math.max(0, Number(this.ytStoryPredictedDurationSeconds || 0) || 0);
                        let durationOverrideApproved = false;

                        if (predictedSeconds > 0 && (predictedSeconds < requestedMinSeconds || predictedSeconds > requestedMaxSeconds)) {
                            const isOver = predictedSeconds > requestedMaxSeconds;
                            const referenceSeconds = isOver ? requestedMaxSeconds : requestedMinSeconds;
                            const deltaSeconds = Math.max(1, Math.round(Math.abs(predictedSeconds - referenceSeconds)));
                            const requestedLabel = requestedMinSeconds === requestedMaxSeconds
                                ? this.formatNarrationDurationHuman(requestedMinSeconds)
                                : `${this.formatNarrationDurationHuman(requestedMinSeconds)} a ${this.formatNarrationDurationHuman(requestedMaxSeconds)}`;
                            const predictedLabel = this.formatNarrationDurationHuman(predictedSeconds);
                            const deltaLabel = this.formatNarrationDurationHuman(deltaSeconds);
                            const directionLabel = isOver ? 'acima' : 'abaixo';''',
        "index/video-submit",
    )

    text = _once(
        text,
        '''                            duration: duration,
                            duration_min: requestedMin,
                            duration_max: requestedMax,
                            duration_override_approved: durationOverrideApproved,
                            auto_upload: !!this.ytStoryAutoUpload,''',
        '''                            duration: duration,
                            duration_min: requestedMin,
                            duration_max: requestedMax,
                            duration_unit: durationUnit,
                            duration_override_approved: durationOverrideApproved,
                            auto_upload: !!this.ytStoryAutoUpload,''',
        "index/video-payload",
    )

    text = _once(
        text,
        '''                            const min = Number(meta.duration_min || meta.durationMin || 0);
                            const max = Number(meta.duration_max || meta.durationMax || 0);
                            if (min) this.ytStoryDurationMin = min;
                            if (max) this.ytStoryDurationMax = max;''',
        '''                            const savedUnit = String(meta.duration_unit || meta.durationUnit || 'minutes').toLowerCase();
                            this.ytStoryDurationUnit = savedUnit === 'seconds' ? 'seconds' : 'minutes';
                            const min = Number(meta.duration_min || meta.durationMin || 0);
                            const max = Number(meta.duration_max || meta.durationMax || 0);
                            if (min) this.ytStoryDurationMin = this.ytStoryDurationUnit === 'seconds' ? Math.round(min * 60) : min;
                            if (max) this.ytStoryDurationMax = this.ytStoryDurationUnit === 'seconds' ? Math.round(max * 60) : max;''',
        "index/draft-load",
    )

    text = _once(
        text,
        '''                        const meta = {
                            duration_min: Number(this.ytStoryDurationMin || 10),
                            duration_max: Number(this.ytStoryDurationMax || this.ytStoryDurationMin || 10),
                            instruction: String(this.ytStoryInstruction || '').trim(),''',
        '''                        const draftDurationUnit = this.ytStoryDurationUnit === 'seconds' ? 'seconds' : 'minutes';
                        const draftMinValue = Number(this.ytStoryDurationMin || (draftDurationUnit === 'seconds' ? 15 : 10));
                        const draftMaxValue = Number(this.ytStoryDurationMax || draftMinValue);
                        const meta = {
                            duration_min: draftDurationUnit === 'seconds' ? draftMinValue / 60 : draftMinValue,
                            duration_max: draftDurationUnit === 'seconds' ? draftMaxValue / 60 : draftMaxValue,
                            duration_unit: draftDurationUnit,
                            instruction: String(this.ytStoryInstruction || '').trim(),''',
        "index/draft-save",
    )

    return text.rstrip() + f"\n<!-- {MARKER} -->\n"


def patch_youtube_router(text: str) -> str:
    if MARKER in text:
        return text

    text = _once(
        text,
        '''    duration: int = 5
    duration_min: Optional[int] = None
    duration_max: Optional[int] = None
    duration_override_approved: bool = False''',
        '''    duration: float = 5
    duration_min: Optional[float] = None
    duration_max: Optional[float] = None
    duration_unit: str = "minutes"
    duration_override_approved: bool = False''',
        "youtube/video-request",
    )

    text = _once(
        text,
        '''        "duration": max(1, min(60, int(payload.get("duration") or 5))),
        "duration_min": max(1, min(60, int(payload.get("duration_min") or payload.get("duration") or 5))),
        "duration_max": max(1, min(60, int(payload.get("duration_max") or payload.get("duration") or 5))),
        "duration_override_approved": bool(payload.get("duration_override_approved")),''',
        '''        "duration": max(5.0 / 60.0, min(60.0, float(payload.get("duration") or 5))),
        "duration_min": max(5.0 / 60.0, min(60.0, float(payload.get("duration_min") or payload.get("duration") or 5))),
        "duration_max": max(5.0 / 60.0, min(60.0, float(payload.get("duration_max") or payload.get("duration") or 5))),
        "duration_unit": _normalize_hash_text(payload.get("duration_unit") or "minutes", lower=True) or "minutes",
        "duration_override_approved": bool(payload.get("duration_override_approved")),''',
        "youtube/dedupe",
    )

    text = _once(
        text,
        '''        requested_minutes = max(1, min(60, requested_minutes))
        try:
            requested_min_minutes = int(getattr(request, "duration_min", None) or requested_minutes)
        except Exception:
            requested_min_minutes = requested_minutes
        try:
            requested_max_minutes = int(getattr(request, "duration_max", None) or requested_minutes)
        except Exception:
            requested_max_minutes = requested_minutes
        requested_min_minutes = max(1, min(60, requested_min_minutes))
        requested_max_minutes = max(requested_min_minutes, min(60, requested_max_minutes))
        duration_override_approved = bool(getattr(request, "duration_override_approved", False))''',
        '''        requested_minutes = max(5.0 / 60.0, min(60.0, float(requested_minutes)))
        try:
            requested_min_minutes = float(getattr(request, "duration_min", None) or requested_minutes)
        except Exception:
            requested_min_minutes = requested_minutes
        try:
            requested_max_minutes = float(getattr(request, "duration_max", None) or requested_minutes)
        except Exception:
            requested_max_minutes = requested_minutes
        requested_min_minutes = max(5.0 / 60.0, min(60.0, requested_min_minutes))
        requested_max_minutes = max(requested_min_minutes, min(60.0, requested_max_minutes))
        duration_unit = str(getattr(request, "duration_unit", "minutes") or "minutes").strip().lower()
        if duration_unit not in {"seconds", "minutes"}:
            duration_unit = "minutes"
        duration_override_approved = bool(getattr(request, "duration_override_approved", False))''',
        "youtube/runtime",
    )

    text = _once(
        text,
        '''            script["duration_min"] = int(requested_min_minutes)
            script["duration_max"] = int(requested_max_minutes)
            script["duration_max_sec"] = int(requested_max_minutes * 60)
            script["target_duration_sec"] = int(requested_minutes * 60)
            script["target_duration_min"] = int(requested_minutes)
            script["duration_override_approved"] = duration_override_approved''',
        '''            script["duration_min"] = float(requested_min_minutes)
            script["duration_max"] = float(requested_max_minutes)
            script["duration_min_sec"] = int(round(requested_min_minutes * 60))
            script["duration_max_sec"] = int(round(requested_max_minutes * 60))
            script["target_duration_sec"] = int(round(requested_minutes * 60))
            script["target_duration_min"] = float(requested_minutes)
            script["duration_unit"] = duration_unit
            script["duration_override_approved"] = duration_override_approved''',
        "youtube/plan",
    )

    text = _once(
        text,
        '''class StoryTextGenerateRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    instruction: str
    duration_min: int = 10
    duration_max: Optional[int] = None

class StoryTextImproveRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    instruction: str = ""
    original_text: str
    duration_min: int = 10
    duration_max: Optional[int] = None''',
        '''class StoryTextGenerateRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    instruction: str
    duration_min: float = 10
    duration_max: Optional[float] = None
    duration_unit: str = "minutes"

class StoryTextImproveRequest(BaseModel):
    kind: str = "story"  # story | devotional | prayer
    instruction: str = ""
    original_text: str
    duration_min: float = 10
    duration_max: Optional[float] = None
    duration_unit: str = "minutes"''',
        "youtube/story-text-models",
    )

    text = _once(
        text,
        '        def _build_story_plan_from_text(story_text: str, duration_minutes: int, kind: str) -> Dict[str, Any]:',
        '        def _build_story_plan_from_text(story_text: str, duration_minutes: float, kind: str) -> Dict[str, Any]:',
        "youtube/story-plan-type",
    )
    text = _once(
        text,
        '            target_words = max(1, int(duration_minutes or 1)) * words_per_minute',
        '            target_words = max(8, int(max(5.0 / 60.0, float(duration_minutes or 1)) * words_per_minute))',
        "youtube/story-plan-words",
    )
    text = text.replace(
        'duration_min_minutes=int(duration_minutes or 10),',
        'duration_min_minutes=max(5.0 / 60.0, float(duration_minutes or 1)),',
    ).replace(
        'duration_max_minutes=int(duration_minutes or 10),',
        'duration_max_minutes=max(5.0 / 60.0, float(duration_minutes or 1)),',
    )

    return text.rstrip() + f"\n# {MARKER}\n"


def patch_story_review_editor(text: str) -> str:
    if MARKER in text:
        return text

    text = text.replace('    min_m: int,\n    max_m: int,', '    min_m: float,\n    max_m: float,')
    text = _once(
        text,
        '''    duration_min_minutes: int = 10,
    duration_max_minutes: Optional[int] = None,''',
        '''    duration_min_minutes: float = 10,
    duration_max_minutes: Optional[float] = None,''',
        "story-editor/signature",
    )
    text = _once(
        text,
        '''    min_m = max(1, min(60, int(duration_min_minutes or 1)))
    max_m = max(min_m, min(60, int(duration_max_minutes or min_m)))
    min_words = max(90, int(min_m * 125))
    max_words = max(min_words + 20, int(max_m * 165))''',
        '''    min_m = max(5.0 / 60.0, min(60.0, float(duration_min_minutes or 1)))
    max_m = max(min_m, min(60.0, float(duration_max_minutes or min_m)))
    min_words = max(8, int(min_m * 125))
    max_words = max(min_words + 5, int(max_m * 165))''',
        "story-editor/word-range",
    )
    text = _once(
        text,
        'DURAÇÃO: {min_m} a {max_m} minuto(s)',
        "{('DURAÇÃO: ' + str(round(min_m * 60)) + ' a ' + str(round(max_m * 60)) + ' segundos') if max_m < 1 else ('DURAÇÃO: ' + format(min_m, 'g') + ' a ' + format(max_m, 'g') + ' minuto(s)')}",
        "story-editor/prompt-unit",
    )
    return text.rstrip() + f"\n\n# {MARKER}\n"


def patch_ai_generator(text: str) -> str:
    if MARKER in text:
        return text

    text = text.replace('duration_min_minutes: int = 10,', 'duration_min_minutes: float = 10,')
    text = text.replace('duration_max_minutes: Optional[int] = None,', 'duration_max_minutes: Optional[float] = None,')
    old = '''        min_m = max(1, int(duration_min_minutes or 1))
        max_m = int(duration_max_minutes) if duration_max_minutes else min_m
        if max_m < min_m:
            max_m = min_m

        # Usando 140 palavras por minuto (ritmo de narração calmo e envolvente)
        min_words = min_m * 140
        max_words = max_m * 160'''
    new = '''        min_m = max(5.0 / 60.0, float(duration_min_minutes or 1))
        max_m = float(duration_max_minutes) if duration_max_minutes else min_m
        if max_m < min_m:
            max_m = min_m

        # Mantém a mesma cadência editorial, inclusive para testes com poucos segundos.
        min_words = max(8, int(round(min_m * 140)))
        max_words = max(min_words + 5, int(round(max_m * 160)))'''
    text = _all(text, old, new, 2, "ai-generator/word-range")
    return text.rstrip() + f"\n\n# {MARKER}\n"


PATCHERS: dict[str, Callable[[str], str]] = {
    "app/static/index.html": patch_index,
    "app/routers/youtube.py": patch_youtube_router,
    "app/services/story_review_editor.py": patch_story_review_editor,
    "app/services/ai_generator.py": patch_ai_generator,
}


def apply(*, write: bool) -> int:
    changed = 0
    for rel_path, patcher in PATCHERS.items():
        path = ROOT / rel_path
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        if transformed != original:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
        if patcher(transformed) != transformed:
            raise PatchError(f"{rel_path}: transformação não é idempotente")
        if rel_path.endswith('.py'):
            compile(transformed, str(path), 'exec')
    mode = "aplicados" if write else "necessários"
    print(f"Duração em segundos: {changed} arquivo(s) {mode}; contratos validados={len(PATCHERS)}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        apply(write=bool(args.apply))
    except (PatchError, SyntaxError) as exc:
        print(f"ERRO DURAÇÃO EM SEGUNDOS: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
