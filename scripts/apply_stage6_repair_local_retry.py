from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_STAGE6_REPAIR_LOCAL_RETRY_V1"


class PatchError(RuntimeError):
    pass


RAW_REPAIR_GUARD_OLD = '''    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")):
        payload["force_render_only"] = False
        return payload'''
RAW_REPAIR_GUARD_NEW = '''    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")) and not bool(payload.get("_repair_stage6_recovery_only")):
        payload["force_render_only"] = False
        return payload'''

PROMOTE_GUARD_OLD = '''    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")):
        return None'''
PROMOTE_GUARD_NEW = '''    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")) and not bool(payload.get("_repair_stage6_recovery_only")):
        return None'''

SOURCES_OLD = '''        result_obj = _recovery_json_dict(getattr(row, "result_json", None))
        unified_obj = _recovery_unified_snapshot(db, str(task_id))
        sources = [result_obj, unified_obj]

        seed_script, script_source = _recovery_choose_script(sources)'''
SOURCES_NEW = '''        result_obj = _recovery_json_dict(getattr(row, "result_json", None))
        unified_obj = _recovery_unified_snapshot(db, str(task_id))
        # CODEXIA_STAGE6_REPAIR_LOCAL_RETRY_V1
        # O payload confirmado da correção é também um checkpoint durável.
        # Ele pode conter imagens que ainda não foram copiadas para result_json
        # ou UnifiedVideo quando a falha ocorreu já no render final.
        retry_payload_source = dict(payload)
        if isinstance(payload.get("seeded_script"), dict):
            retry_payload_source["script"] = dict(payload.get("seeded_script") or {})
        sources = [result_obj, unified_obj, retry_payload_source]

        seed_script, script_source = _recovery_choose_script(sources)'''

VISUAL_COUNTS_OLD = '''        visual_source_counts = {"video_task": 0, "unified_video": 0}
        for source_index, source in enumerate(sources):
            source_name = "video_task" if source_index == 0 else "unified_video"'''
VISUAL_COUNTS_NEW = '''        visual_source_counts = {"video_task": 0, "unified_video": 0, "retry_payload": 0}
        source_names = ("video_task", "unified_video", "retry_payload")
        for source_index, source in enumerate(sources):
            source_name = source_names[source_index] if source_index < len(source_names) else "retry_payload"'''

CONFIRMED_OLD = '''        if ready_repair_confirmed:
            payload["force_render_only"] = False
            payload.pop("_recovery_block_paid_regeneration", None)
            payload.pop("_recovery_missing_assets", None)
        else:
            payload = _maybe_enable_render_only_flags(payload, task_id)'''
CONFIRMED_NEW = '''        repair_stage6_recovery_only = False
        if ready_repair_confirmed:
            try:
                current_retry_task = get_task(task_id) or {}
                current_retry_result = current_retry_task.get("result") if isinstance(current_retry_task.get("result"), dict) else {}
                current_checkpoint = current_retry_result.get("recovery_checkpoint") if isinstance(current_retry_result.get("recovery_checkpoint"), dict) else {}
                current_unified_meta = current_checkpoint.get("unified_meta") if isinstance(current_checkpoint.get("unified_meta"), dict) else {}
                current_stage_hint = str(current_unified_meta.get("current_step") or current_checkpoint.get("stage") or "").strip().lower()
                repair_stage6_recovery_only = current_stage_hint == "stage_6_render"
            except Exception:
                repair_stage6_recovery_only = False

        if ready_repair_confirmed and repair_stage6_recovery_only:
            # A correção editorial já gerou sua nova mídia e chegou ao render final.
            # A partir daqui retry é somente recuperação local: primeiro tenta
            # promover o MP4 novo; se ele não estiver íntegro, reutiliza áudio e
            # imagens da própria tentativa para refazer apenas o render.
            payload["_repair_stage6_recovery_only"] = True
            payload = _maybe_enable_render_only_flags(payload, task_id)
            if bool(payload.get("force_render_only")) and not bool(payload.get("_recovery_block_paid_regeneration")):
                payload["repair_mode"] = False
                payload["repair_regenerate_audio"] = False
                payload["repair_exclude_video"] = False
                payload["repair_complete_visuals"] = False
                seeded_stage6 = payload.get("seeded_script") if isinstance(payload.get("seeded_script"), dict) else None
                if isinstance(seeded_stage6, dict):
                    seeded_stage6 = dict(seeded_stage6)
                    seeded_stage6["repair_regenerate_audio"] = False
                    seeded_stage6["repair_exclude_video"] = False
                    seeded_stage6["repair_complete_visuals"] = False
                    payload["seeded_script"] = seeded_stage6
        elif ready_repair_confirmed:
            payload["force_render_only"] = False
            payload.pop("_recovery_block_paid_regeneration", None)
            payload.pop("_recovery_missing_assets", None)
        else:
            payload = _maybe_enable_render_only_flags(payload, task_id)'''


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
    if "CODEXIA_READY_VIDEO_ASSET_REPAIR_V3" not in text:
        raise PatchError("ready-video repair V3 deve ser aplicado antes do retry stage6")
    text = _replace_once(text, RAW_REPAIR_GUARD_OLD, RAW_REPAIR_GUARD_NEW, "guard de render-only do reparo")
    text = _replace_once(text, PROMOTE_GUARD_OLD, PROMOTE_GUARD_NEW, "guard de promoção do MP4 antigo")
    text = _replace_once(text, SOURCES_OLD, SOURCES_NEW, "inventário do payload de retry")
    text = _replace_once(text, VISUAL_COUNTS_OLD, VISUAL_COUNTS_NEW, "origem visual do payload de retry")
    text = _replace_once(text, CONFIRMED_OLD, CONFIRMED_NEW, "retry confirmado V3")
    text = text.rstrip() + f"\n\n# {MARKER}\n"
    return text


def apply() -> None:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("patch stage6 não é idempotente")
    if transformed != original:
        YOUTUBE.write_text(transformed, encoding="utf-8")


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    required = (
        MARKER,
        '_repair_stage6_recovery_only',
        'current_stage_hint == "stage_6_render"',
        'retry_payload_source["script"]',
        '"retry_payload": 0',
        'payload["repair_regenerate_audio"] = False',
        'payload["repair_exclude_video"] = False',
        'payload["repair_complete_visuals"] = False',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("retry local stage6 incompleto: " + ", ".join(missing))
    if RAW_REPAIR_GUARD_OLD in text or PROMOTE_GUARD_OLD in text or CONFIRMED_OLD in text:
        raise PatchError("guards antigos de reparo ainda ativos")
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
        print(f"ERRO STAGE6 REPAIR LOCAL RETRY: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
