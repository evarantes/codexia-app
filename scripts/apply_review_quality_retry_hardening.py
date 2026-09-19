from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_REVIEW_QUALITY_RETRY_V1"


class PatchError(RuntimeError):
    pass


PAYLOAD_HELPERS = '''def _is_review_quality_retry_payload(payload: Any) -> bool:
    value = payload if isinstance(payload, dict) else {}
    return bool(
        value.get("director_quality_required")
        and value.get("force_regenerate")
        and str(value.get("review_feedback") or "").strip()
    )


def _prepare_review_quality_retry_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Force a fresh quality pass after human rejection.

    A rejected video must never be converted into render-only recovery: that
    would preserve the short narration and repeated images that were rejected.
    """
    prepared = dict(payload or {})
    prepared["force_regenerate"] = True
    prepared["force_reuse_assets"] = False
    prepared["force_render_only"] = False
    prepared["director_quality_required"] = True
    prepared["editorial_reviewed"] = False
    prepared["editorial_review_ready"] = False
    for key in (
        "seeded_script",
        "selected_images",
        "reuse_audio_from",
        "intelligent_visual_optimization",
        "lightweight_recovery_render_confirmed",
        "_repair_stage6_recovery_only",
        "_production_manifest_recovery",
        "_recovery_generate_missing_images_only",
        "_recovery_missing_image_count",
        "_recovery_block_paid_regeneration",
        "_recovery_missing_assets",
    ):
        prepared.pop(key, None)
    return prepared


'''

HELPER_ANCHOR = '''def _intelligent_retry_visual_materials(task_id: str, payload_override: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
'''

QUALITY_PLAN_ANCHOR = '''        if isinstance(payload_override, dict):
            payload.update(payload_override)

        seed_script = payload.get("seeded_script") if isinstance(payload.get("seeded_script"), dict) else None'''

QUALITY_PLAN_REPLACEMENT = '''        if isinstance(payload_override, dict):
            payload.update(payload_override)

        # CODEXIA_REVIEW_QUALITY_RETRY_V1
        # Human review outranks the zero-cost recovery optimizer. Reusing the
        # rejected narration/images would make the requested correction
        # impossible and could falsely promise quality with zero new assets.
        if _is_review_quality_retry_payload(payload):
            return {
                "requires_confirmation": False,
                "optimization_required": False,
                "quality_correction_required": True,
                "retry_mode": "fresh_quality_regeneration",
                "message": (
                    "O vídeo foi reprovado na revisão. O Codexia fará uma nova passagem de qualidade "
                    "para corrigir duração, variedade visual e sincronização das legendas."
                ),
            }, {}

        seed_script = payload.get("seeded_script") if isinstance(payload.get("seeded_script"), dict) else None'''

PROMOTE_ANCHOR = '''        final_render_recovery = _recovery_try_promote_final_render(payload, task_id)
        if isinstance(final_render_recovery, dict) and final_render_recovery.get("recovered"):
            return final_render_recovery
        if isinstance(final_render_recovery, dict) and final_render_recovery.get("blocked"):
            raise HTTPException(status_code=409, detail=str(final_render_recovery.get("message") or "Recuperação bloqueada."))'''

PROMOTE_REPLACEMENT = '''        quality_correction_retry = _is_review_quality_retry_payload(payload)
        if quality_correction_retry:
            payload = _prepare_review_quality_retry_payload(payload)
            final_render_recovery = None
        else:
            final_render_recovery = _recovery_try_promote_final_render(payload, task_id)
        if isinstance(final_render_recovery, dict) and final_render_recovery.get("recovered"):
            return final_render_recovery
        if isinstance(final_render_recovery, dict) and final_render_recovery.get("blocked"):
            raise HTTPException(status_code=409, detail=str(final_render_recovery.get("message") or "Recuperação bloqueada."))'''

RESET_ANCHOR = '''        reset = reset_task_for_retry(
            task_id,
            progress=resume_progress,
            message="Retomada preparada com reaproveitamento dos ativos; aguardando worker CX33...",
        )'''

RESET_REPLACEMENT = '''        retry_message = (
            "Correção de qualidade preparada com nova narração, novas imagens e legendas sincronizadas; aguardando worker CX33..."
            if quality_correction_retry
            else "Retomada preparada com reaproveitamento dos ativos; aguardando worker CX33..."
        )
        reset = reset_task_for_retry(
            task_id,
            progress=resume_progress,
            message=retry_message,
        )'''

PIPELINE_ANCHOR = '''                message="Retomada preparada, reutilizando os ativos disponíveis e aguardando worker CX33.",
                merge_result={
                    "recovery": {
                        "same_task": True,
                        "force_reuse_assets": True,
                        "force_render_only": bool(payload.get("force_render_only")),
                    }
                },'''

PIPELINE_REPLACEMENT = '''                message=retry_message,
                merge_result={
                    "recovery": {
                        "same_task": True,
                        "quality_correction": bool(quality_correction_retry),
                        "force_reuse_assets": bool(payload.get("force_reuse_assets")),
                        "force_render_only": bool(payload.get("force_render_only")),
                    }
                },'''

UV_ANCHOR = '''            if uv is not None:
                uv.force_reuse_assets = True
                uv.force_render_only = bool(payload.get("force_render_only"))
                pipeline_db.commit()'''

UV_REPLACEMENT = '''            if uv is not None:
                uv.force_reuse_assets = bool(payload.get("force_reuse_assets"))
                uv.force_render_only = bool(payload.get("force_render_only"))
                pipeline_db.commit()'''

RETURN_ANCHOR = '''        return {
            "message": "Mesma tarefa reiniciada com reaproveitamento de ativos.",
            "task_id": task_id,
            "reused_task": True,
            "reuse_assets": True,
            "render_only": bool(payload.get("force_render_only")),
            "pipeline": "unified_video_pipeline",
        }'''

RETURN_REPLACEMENT = '''        return {
            "message": retry_message,
            "task_id": task_id,
            "reused_task": True,
            "quality_correction": bool(quality_correction_retry),
            "reuse_assets": bool(payload.get("force_reuse_assets")),
            "render_only": bool(payload.get("force_render_only")),
            "pipeline": "unified_video_pipeline",
        }'''


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def patch_youtube(text: str) -> str:
    if MARKER in text:
        return text
    if HELPER_ANCHOR not in text:
        raise PatchError("otimizador de retry deve ser aplicado antes da correção editorial")
    text = text.replace(HELPER_ANCHOR, PAYLOAD_HELPERS + HELPER_ANCHOR, 1)
    text = _replace_once(text, QUALITY_PLAN_ANCHOR, QUALITY_PLAN_REPLACEMENT, "plano de correção")
    text = _replace_once(text, PROMOTE_ANCHOR, PROMOTE_REPLACEMENT, "bloqueio de promoção do vídeo reprovado")
    text = _replace_once(text, RESET_ANCHOR, RESET_REPLACEMENT, "mensagem de reinício")
    text = _replace_once(text, PIPELINE_ANCHOR, PIPELINE_REPLACEMENT, "estado unificado da correção")
    text = _replace_once(text, UV_ANCHOR, UV_REPLACEMENT, "flags do registro unificado")
    text = _replace_once(text, RETURN_ANCHOR, RETURN_REPLACEMENT, "resposta do retry")
    return text.rstrip() + f"\n\n# {MARKER}\n"


def apply() -> None:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("hardening de retry editorial não é idempotente")
    if transformed != original:
        YOUTUBE.write_text(transformed, encoding="utf-8")


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    required = (
        MARKER,
        "quality_correction_required",
        "fresh_quality_regeneration",
        "_prepare_review_quality_retry_payload",
        'prepared["force_reuse_assets"] = False',
        'prepared.pop(key, None)',
        "if quality_correction_retry",
        'uv.force_reuse_assets = bool(payload.get("force_reuse_assets"))',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("retry editorial incompleto: " + ", ".join(missing))
    compile(text, str(YOUTUBE), "exec")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO REVIEW QUALITY RETRY: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
