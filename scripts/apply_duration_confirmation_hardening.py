#!/usr/bin/env python3
"""Aplica confirmação humana para desvios de duração em vídeos narrados.

A duração escolhida pelo usuário é uma meta editorial, não uma causa automática
de falha. A previsão continua protegendo custos, mas qualquer desvio para cima
ou para baixo é apresentado antes de enfileirar o vídeo. Se o usuário confirmar,
o backend registra a autorização e permite que o pipeline prossiga.

O patch é determinístico, idempotente e roda no CI/build sem reescrever os
arquivos legados grandes no repositório.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho original, encontrado {count}")
    return text.replace(old, new, 1)


def patch_index(text: str) -> str:
    # A previsão nunca deve sobrescrever a faixa escolhida pelo usuário.
    text = _replace_once(
        text,
        """                ytStoryPredictedDurationMinutesValue: {
                    handler(value) {
                        this.ytStoryVideoDuration = Math.max(1, Number(value || 0) || 1);
                    },
                    immediate: true
                },""",
        """                ytStoryPredictedDurationMinutesValue: {
                    handler() {
                        // A previsão é apenas informativa. A faixa pedida pelo usuário é soberana.
                    },
                    immediate: true
                },""",
        label="index/prediction-does-not-overwrite-request",
    )
    text = _replace_once(
        text,
        "                            this.ytStoryVideoDuration = this.ytStoryPredictedDurationMinutesValue || this.ytStoryVideoDuration;",
        "                            // Não substitui a duração solicitada pela duração prevista.",
        label="index/generated-text-preserves-requested-duration",
    )

    # Confirmação acontece antes de submitVideoGeneration, portanto antes de
    # criar tarefa e antes de qualquer mídia paga.
    text = _replace_once(
        text,
        """                        const duration = Number(this.ytStoryPredictedDurationMinutesValue || this.ytStoryVideoDuration || this.ytStoryDurationMax || this.ytStoryDurationMin || 10);
                        const selectedImages = this.selectedImageUrlsAll();""",
        """                        const requestedMin = Math.max(1, Math.min(60, Number(this.ytStoryDurationMin || 1) || 1));
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
                            const directionLabel = isOver ? 'acima' : 'abaixo';
                            const confirmed = window.confirm(
                                `Aviso de duração do vídeo\n\n` +
                                `Você pediu aproximadamente ${requestedLabel}.\n` +
                                `O roteiro atual está previsto para ${predictedLabel}, cerca de ${deltaLabel} ${directionLabel} da faixa solicitada.\n\n` +
                                `O tempo é apenas uma referência editorial. Se o conteúdo estiver coerente e com começo, meio e fim, você pode continuar assim mesmo.\n\n` +
                                `OK = Continuar assim mesmo\nCancelar = Voltar e ajustar o roteiro`
                            );
                            if (!confirmed) {
                                return;
                            }
                            durationOverrideApproved = true;
                        }

                        const selectedImages = this.selectedImageUrlsAll();""",
        label="index/duration-warning-before-submit",
    )
    text = _replace_once(
        text,
        """                            duration: duration,
                            auto_upload: !!this.ytStoryAutoUpload,""",
        """                            duration: duration,
                            duration_min: requestedMin,
                            duration_max: requestedMax,
                            duration_override_approved: durationOverrideApproved,
                            auto_upload: !!this.ytStoryAutoUpload,""",
        label="index/send-duration-range-and-approval",
    )
    return text


def patch_youtube_router(text: str) -> str:
    text = _replace_once(
        text,
        """    duration: int = 5
    auto_upload: bool = False""",
        """    duration: int = 5
    duration_min: Optional[int] = None
    duration_max: Optional[int] = None
    duration_override_approved: bool = False
    auto_upload: bool = False""",
        label="youtube/video-request-duration-confirmation-fields",
    )

    # A autorização participa da identidade da tentativa para que uma tarefa
    # previamente bloqueada sem autorização não seja devolvida por deduplicação.
    text = _replace_once(
        text,
        '        "duration": max(1, min(60, int(payload.get("duration") or 5))),\n',
        '        "duration": max(1, min(60, int(payload.get("duration") or 5))),\n'
        '        "duration_min": max(1, min(60, int(payload.get("duration_min") or payload.get("duration") or 5))),\n'
        '        "duration_max": max(1, min(60, int(payload.get("duration_max") or payload.get("duration") or 5))),\n'
        '        "duration_override_approved": bool(payload.get("duration_override_approved")),\n',
        label="youtube/dedupe-respects-duration-approval",
    )

    text = _replace_once(
        text,
        '''        requested_minutes = max(1, min(60, requested_minutes))
        default_voice_style = "soft_prayer" if kind_norm == "prayer" else "human"''',
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
        duration_override_approved = bool(getattr(request, "duration_override_approved", False))
        default_voice_style = "soft_prayer" if kind_norm == "prayer" else "human"''',
        label="youtube/normalize-duration-range",
    )

    text = _replace_once(
        text,
        """            script["target_duration_sec"] = int(requested_minutes * 60)
            script["target_duration_min"] = int(requested_minutes)
            script["kind"] = kind_norm""",
        """            script["duration_min"] = int(requested_min_minutes)
            script["duration_max"] = int(requested_max_minutes)
            script["duration_max_sec"] = int(requested_max_minutes * 60)
            script["target_duration_sec"] = int(requested_minutes * 60)
            script["target_duration_min"] = int(requested_minutes)
            script["duration_override_approved"] = duration_override_approved
            script["kind"] = kind_norm""",
        label="youtube/propagate-duration-confirmation-to-plan",
    )
    return text


def patch_channel_excellence_guard(text: str) -> str:
    # O teto informado pelo usuário é a referência para excesso de duração.
    text = _replace_once(
        text,
        '    for key in ("target_duration_sec", "duration_max_sec", "duration_sec"):\n',
        '    for key in ("duration_max_sec", "target_duration_sec", "duration_sec"):\n',
        label="guard/prefer-duration-max-seconds",
    )
    text = _replace_once(
        text,
        '    for key in ("target_duration_min", "duration_max", "duration_min", "duration"):\n',
        '    for key in ("duration_max", "target_duration_min", "duration_min", "duration"):\n',
        label="guard/prefer-duration-max-minutes",
    )

    text = _replace_once(
        text,
        """            duration_preflight = _duration_preflight(guarded)
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
                )""",
        """            duration_preflight = _duration_preflight(guarded)
            duration_override_approved = bool(guarded.get("duration_override_approved"))
            duration_was_outside_tolerance = bool(
                duration_preflight.get("checked") and not duration_preflight.get("passed")
            )
            duration_preflight["overridden_by_user"] = bool(
                duration_override_approved and duration_was_outside_tolerance
            )
            if duration_preflight["overridden_by_user"]:
                duration_preflight["approval_source"] = "user_confirmation"
            if (
                _enabled("ENABLE_DURATION_SANITY_PREFLIGHT", "true")
                and duration_was_outside_tolerance
                and not duration_override_approved
            ):
                raise RuntimeError(
                    "Roteiro fora da tolerância editorial de duração antes de gerar mídia paga: "
                    f"alvo {int(duration_preflight.get('target_sec') or 0)}s, "
                    f"estimado {int(duration_preflight.get('estimated_sec') or 0)}s, "
                    f"limite flexível {int(duration_preflight.get('max_sec') or 0)}s. "
                    "Confirme o aviso de duração para continuar assim mesmo ou ajuste o roteiro."
                )""",
        label="guard/user-can-confirm-duration-deviation",
    )
    return text


PATCHERS: dict[str, Callable[[str], str]] = {
    "app/static/index.html": patch_index,
    "app/routers/youtube.py": patch_youtube_router,
    "app/services/channel_excellence_guard.py": patch_channel_excellence_guard,
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
        second = patcher(transformed)
        if second != transformed:
            raise PatchError(f"{rel_path}: transformação não é idempotente")
    mode = "aplicados" if write else "necessários"
    print(f"Confirmação de duração: {changed} arquivo(s) {mode}; contratos validados={len(PATCHERS)}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="grava as transformações")
    parser.add_argument("--check", action="store_true", help="somente valida os padrões")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        apply(write=bool(args.apply))
    except PatchError as exc:
        print(f"ERRO DE CONFIRMAÇÃO DE DURAÇÃO: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
