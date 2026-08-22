from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_MANAGER = ROOT / "app/services/task_manager.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
VIDEO_GENERATOR = ROOT / "app/services/video_generator.py"
SERVICE = ROOT / "app/services/production_manifest.py"
MARKER_TASK = "# CODEXIA_PRODUCTION_MANIFEST_TASK_SYNC_V1"
MARKER_YT = "# CODEXIA_PRODUCTION_MANIFEST_RECOVERY_V1"
MARKER_VIDEO = "# CODEXIA_PARTIAL_IMAGE_RECOVERY_V1"
MARKER_SERVICE = "# CODEXIA_PARTIAL_IMAGE_RECOVERY_PAYLOAD_V1"


TASK_OLD = '''def _redis_set(task_id: str, data: Dict[str, Any]):\n    if not _redis_conn:\n        return\n    try:\n        _redis_conn.set(_REDIS_PREFIX + task_id, json.dumps(data), ex=60 * 60)\n    except Exception:\n        pass'''

TASK_NEW = '''def _redis_set(task_id: str, data: Dict[str, Any]):\n    # CODEXIA_PRODUCTION_MANIFEST_TASK_SYNC_V1\n    # Persist every canonical task snapshot before Redis so a cache outage,\n    # deploy or late quality-gate failure cannot orphan already-paid assets.\n    try:\n        from app.services.production_manifest import sync_task_snapshot\n        sync_task_snapshot(str(task_id), data if isinstance(data, dict) else {})\n    except Exception:\n        # Manifest persistence is protective and must never make the pipeline fail.\n        pass\n    if not _redis_conn:\n        return\n    try:\n        _redis_conn.set(_REDIS_PREFIX + task_id, json.dumps(data), ex=60 * 60)\n    except Exception:\n        pass'''


IMPORT_OLD = '''from app.services.media_probe import media_durations_match, probe_media_file'''
IMPORT_NEW = '''from app.services.media_probe import media_durations_match, probe_media_file\nfrom app.services.production_manifest import (\n    build_recovery_plan,\n    confirm_or_prepare_partial_recovery,\n    recovery_confirmation_message,\n    recovery_payload_patch,\n    recovery_ready_message,\n)'''


# The final-render recovery patch runs before this hardening and inserts its MP4
# rescue call between _maybe_enable_render_only_flags() and this paid guard.
# Therefore anchor only the guard body, not the preceding payload line.
PAID_GUARD_OLD = '''        if bool(payload.get("_recovery_block_paid_regeneration")):\n            missing = [str(item) for item in (payload.get("_recovery_missing_assets") or []) if str(item or "").strip()]\n            missing_label = ", ".join(missing) if missing else "ativos necessários"\n            recovery_message = (\n                "Recuperação segura bloqueada antes de novas chamadas pagas: "\n                f"faltam {missing_label}. Os ativos existentes foram preservados. "\n                "Nenhuma nova mídia foi gerada nesta tentativa."\n            )\n            update_task(task_id, message=recovery_message)\n            raise HTTPException(status_code=409, detail=recovery_message)\n        try:\n            VideoRequest(**payload)'''

PAID_GUARD_NEW = '''        if bool(payload.get("_recovery_block_paid_regeneration")):\n            missing = [str(item) for item in (payload.get("_recovery_missing_assets") or []) if str(item or "").strip()]\n            # O manifesto é a segunda fonte de verdade. Mesmo quando o registro\n            # legado perdeu a referência de áudio/imagens, ativos físicos já\n            # preservados podem permitir rerender sem custo ou recuperação\n            # parcial somente das imagens ausentes.\n            try:\n                manifest_plan = build_recovery_plan(task_id, payload_override=payload)\n            except Exception:\n                manifest_plan = {}\n            manifest_action = str(manifest_plan.get("action") or "") if isinstance(manifest_plan, dict) else ""\n            if manifest_action == "rerender_without_paid_media":\n                payload = recovery_payload_patch(task_id, payload, manifest_plan)\n                ready_message = (\n                    "Recuperação sem novas chamadas pagas: roteiro, áudio e imagens preservados serão reutilizados; "\n                    "somente a composição/renderização será refeita."\n                )\n                update_task(task_id, message=ready_message)\n            elif manifest_action == "regenerate_missing_images":\n                try:\n                    manifest_decision = confirm_or_prepare_partial_recovery(task_id, payload)\n                except Exception:\n                    manifest_decision = {"allow": False, "plan": manifest_plan, "reason": "manifest_error"}\n                confirmed_plan = manifest_decision.get("plan") if isinstance(manifest_decision, dict) else manifest_plan\n                if isinstance(manifest_decision, dict) and bool(manifest_decision.get("allow")):\n                    recovered_payload = manifest_decision.get("payload")\n                    if isinstance(recovered_payload, dict):\n                        payload = recovered_payload\n                    ready_message = recovery_ready_message(confirmed_plan if isinstance(confirmed_plan, dict) else {})\n                    update_task(task_id, message=ready_message)\n                else:\n                    recovery_message = recovery_confirmation_message(confirmed_plan if isinstance(confirmed_plan, dict) else manifest_plan)\n                    update_task(task_id, message=recovery_message)\n                    raise HTTPException(status_code=409, detail=recovery_message)\n            else:\n                missing_label = ", ".join(missing) if missing else "ativos necessários"\n                recovery_message = (\n                    "Recuperação segura bloqueada antes de novas chamadas pagas: "\n                    f"faltam {missing_label}. Os ativos existentes foram preservados. "\n                    "Nenhuma nova mídia foi gerada nesta tentativa."\n                )\n                update_task(task_id, message=recovery_message)\n                raise HTTPException(status_code=409, detail=recovery_message)\n        try:\n            VideoRequest(**payload)'''

PAID_GUARD_NEW = PAID_GUARD_NEW.replace(
    '            elif manifest_action == "regenerate_missing_images":',
    '''            elif manifest_action in {
                "regenerate_missing_images",
                "rebuild_untrusted_audio",
                "rebuild_missing_audio",
                "rebuild_audio_and_missing_images",
            }:''',
)


SCOPE_OLD = '''            if bool(payload.get("_recovery_block_paid_regeneration")):\n                update_task(task_id, message=message)\n                return {\n                    "recovered": False,\n                    "blocked": True,\n                    "task_id": task_id,\n                    "message": message,\n                    "reason": base_reason,\n                }\n            return None'''

SCOPE_NEW = '''            if bool(payload.get("_recovery_block_paid_regeneration")):\n                # Deixe o manifesto decidir se há recuperação parcial ou\n                # rerender sem custo. Nenhuma chamada paga acontece nesta fase.\n                try:\n                    manifest_plan = build_recovery_plan(task_id, payload_override=payload)\n                except Exception:\n                    manifest_plan = {}\n                manifest_action = str(manifest_plan.get("action") or "") if isinstance(manifest_plan, dict) else ""\n                if manifest_action not in {"regenerate_missing_images", "rerender_without_paid_media"}:\n                    update_task(task_id, message=message)\n                    return {\n                        "recovered": False,\n                        "blocked": True,\n                        "task_id": task_id,\n                        "message": message,\n                        "reason": base_reason,\n                    }\n            return None'''

SCOPE_NEW = SCOPE_NEW.replace(
    '{"regenerate_missing_images", "rerender_without_paid_media"}',
    '''{
                    "regenerate_missing_images",
                    "rerender_without_paid_media",
                    "rebuild_untrusted_audio",
                    "rebuild_missing_audio",
                    "rebuild_audio_and_missing_images",
                }''',
)


SERVICE_PARTIAL_OLD = '''    elif plan.get("action") == "regenerate_missing_images":\n        patched["force_render_only"] = False\n        patched["_recovery_generate_missing_images_only"] = True\n        patched["_recovery_missing_image_count"] = int(plan.get("missing_image_count") or 0)\n        patched.pop("_recovery_block_paid_regeneration", None)\n        patched.pop("_recovery_missing_assets", None)'''

SERVICE_PARTIAL_NEW = '''    elif plan.get("action") == "regenerate_missing_images":\n        patched["force_render_only"] = False\n        patched["_recovery_generate_missing_images_only"] = True\n        patched["_recovery_missing_image_count"] = int(plan.get("missing_image_count") or 0)\n        # CODEXIA_PARTIAL_IMAGE_RECOVERY_PAYLOAD_V1\n        # A flag precisa viajar dentro do seeded_script porque o contrato\n        # Pydantic ignora chaves privadas extras do payload. O renderer então\n        # mantém todos os grupos visuais esperados, usa as imagens existentes\n        # em seus primeiros grupos e gera somente os grupos ainda sem imagem.\n        seeded = patched.get("seeded_script") if isinstance(patched.get("seeded_script"), dict) else {}\n        if seeded:\n            seeded = dict(seeded)\n            seeded["_partial_image_recovery"] = {\n                "enabled": True,\n                "existing_image_count": len(existing_images),\n                "expected_image_count": int(plan.get("expected_image_count") or 0),\n                "missing_image_count": int(plan.get("missing_image_count") or 0),\n                "estimated_image_cost_usd": float(plan.get("estimated_image_cost_usd") or 0.0),\n                "estimated_image_cost_brl": float(plan.get("estimated_image_cost_brl") or 0.0),\n                "plan_hash": str(plan.get("plan_hash") or ""),\n            }\n            patched["seeded_script"] = seeded\n        patched.pop("_recovery_block_paid_regeneration", None)\n        patched.pop("_recovery_missing_assets", None)'''


SEED_AUDIO_OLD = '''                try:\n                    seed_audio_path = str(((seed_render_report.get("audio_generation") or {}).get("output_path") or "")).strip()\n                except Exception:\n                    seed_audio_path = ""\n                seed_narration_text = ""'''

SEED_AUDIO_NEW = '''                try:\n                    seed_audio_path = str(((seed_render_report.get("audio_generation") or {}).get("output_path") or "")).strip()\n                except Exception:\n                    seed_audio_path = ""\n                # Manifest recovery may carry the durable audio only in the\n                # request. Prefer it when legacy task JSON lost the reference.\n                if not seed_audio_path and isinstance(getattr(request, "reuse_audio_from", None), dict):\n                    reuse_audio = dict(getattr(request, "reuse_audio_from", None) or {})\n                    for reuse_key in ("output_path", "final_audio_path", "audio_path"):\n                        candidate = str(reuse_audio.get(reuse_key) or "").strip()\n                        if candidate and _file_ok(candidate):\n                            seed_audio_path = candidate\n                            break\n                seed_narration_text = ""'''

SEED_AUDIO_NEW = SEED_AUDIO_NEW.replace(
    '                seed_narration_text = ""',
    '''                request_seed_for_policy = request.seeded_script if isinstance(getattr(request, "seeded_script", None), dict) else {}
                recovery_policy = request_seed_for_policy.get("_manifest_recovery_policy") if isinstance(request_seed_for_policy, dict) else {}
                if isinstance(recovery_policy, dict) and bool(recovery_policy.get("rebuild_audio")):
                    # A confirmação explícita autoriza reconstruir a narração;
                    # nunca recoloque o áudio legado apenas porque ainda existe.
                    seed_audio_path = ""
                seed_narration_text = ""''',
)


SEED_SCRIPT_OLD = '''                elif bool(getattr(request, "force_reuse_assets", False)) and seed_script_ok:\n                    script = dict(seed_script or {})\n                    reused: List[str] = ["roteiro"]'''

SEED_SCRIPT_NEW = '''                elif bool(getattr(request, "force_reuse_assets", False)) and seed_script_ok:\n                    script = dict(seed_script or {})\n                    # Preserve recovery metadata supplied by the durable\n                    # manifest even when the canonical task JSON contributes\n                    # the actual script body.\n                    request_seed = request.seeded_script if isinstance(getattr(request, "seeded_script", None), dict) else {}\n                    partial_meta = request_seed.get("_partial_image_recovery") if isinstance(request_seed, dict) else None\n                    if isinstance(partial_meta, dict):\n                        script["_partial_image_recovery"] = dict(partial_meta)\n                    reused: List[str] = ["roteiro"]'''

SEED_SCRIPT_NEW = SEED_SCRIPT_NEW.replace(
    '                    reused: List[str] = ["roteiro"]',
    '''                    recovery_policy = request_seed.get("_manifest_recovery_policy") if isinstance(request_seed, dict) else None
                    if isinstance(recovery_policy, dict):
                        script["_manifest_recovery_policy"] = dict(recovery_policy)
                    reused: List[str] = ["roteiro"]''',
)


VIDEO_SETUP_OLD = '''            selected_image_paths = []\n            selected_primary_path = None\n            scene_image_pool = []\n            scene_image_seen = set()\n            scene_reuse_counts = {}\n            selected_raw = plan.get("selected_images") or plan.get("images") or []'''

VIDEO_SETUP_NEW = '''            selected_image_paths = []\n            selected_primary_path = None\n            scene_image_pool = []\n            scene_image_seen = set()\n            scene_reuse_counts = {}\n            # CODEXIA_PARTIAL_IMAGE_RECOVERY_V1\n            partial_image_meta = plan.get("_partial_image_recovery") if isinstance(plan, dict) else {}\n            partial_image_recovery = bool(\n                isinstance(partial_image_meta, dict)\n                and partial_image_meta.get("enabled")\n            )\n            selected_raw = plan.get("selected_images") or plan.get("images") or []'''

VIDEO_GROUP_OLD = '''                selected_image_count=len(selected_image_paths),'''
VIDEO_GROUP_NEW = '''                # During partial recovery, existing images are a prefix of the\n                # full visual plan; they must not shrink the number of groups.\n                selected_image_count=(0 if partial_image_recovery else len(selected_image_paths)),'''

VIDEO_SCENE_OLD = '''                if selected_image_paths:\n                    bg_image_path = self._selected_image_for_visual_group(\n                        selected_image_paths,\n                        visual_group_id,\n                    )'''

VIDEO_SCENE_NEW = '''                if selected_image_paths and (\n                    (not partial_image_recovery) or visual_group_id < len(selected_image_paths)\n                ):\n                    bg_image_path = self._selected_image_for_visual_group(\n                        selected_image_paths,\n                        visual_group_id,\n                    )'''

VIDEO_OPENING_OLD = '''                selected_primary_path=selected_primary_path,\n                cover_image_path=cover_image_path,'''
VIDEO_OPENING_NEW = '''                selected_primary_path=selected_primary_path,\n                # Reuse the preserved first image as the opening during partial\n                # recovery instead of paying for a new thematic cover.\n                cover_image_path=(selected_primary_path if partial_image_recovery and selected_primary_path else cover_image_path),'''


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1), True


def apply() -> None:
    task_text = TASK_MANAGER.read_text(encoding="utf-8")
    task_text, _ = _replace_once(task_text, TASK_OLD, TASK_NEW, "task manifest sync")
    TASK_MANAGER.write_text(task_text, encoding="utf-8")

    service_text = SERVICE.read_text(encoding="utf-8")
    service_text, _ = _replace_once(service_text, SERVICE_PARTIAL_OLD, SERVICE_PARTIAL_NEW, "partial recovery payload")
    SERVICE.write_text(service_text, encoding="utf-8")

    yt_text = YOUTUBE.read_text(encoding="utf-8")
    if "CODEXIA_RECOVERY_CHECKPOINT_V3_START" not in yt_text:
        raise PatchError("recovery checkpoint v3 deve ser aplicado antes do manifesto")
    if "CODEXIA_FINAL_RENDER_RECOVERY_SCOPE_V1" not in yt_text:
        raise PatchError("final render scope v1 deve ser aplicado antes do manifesto")
    yt_text, _ = _replace_once(yt_text, IMPORT_OLD, IMPORT_NEW, "production manifest import")
    yt_text, _ = _replace_once(yt_text, PAID_GUARD_OLD, PAID_GUARD_NEW, "paid recovery confirmation")
    yt_text, _ = _replace_once(yt_text, SCOPE_OLD, SCOPE_NEW, "final render partial recovery scope")
    yt_text, _ = _replace_once(yt_text, SEED_AUDIO_OLD, SEED_AUDIO_NEW, "manifest audio seed fallback")
    yt_text, _ = _replace_once(yt_text, SEED_SCRIPT_OLD, SEED_SCRIPT_NEW, "manifest partial metadata")
    if MARKER_YT not in yt_text:
        yt_text = yt_text.rstrip() + f"\n\n{MARKER_YT}\n"
    YOUTUBE.write_text(yt_text, encoding="utf-8")

    video_text = VIDEO_GENERATOR.read_text(encoding="utf-8")
    video_text, _ = _replace_once(video_text, VIDEO_SETUP_OLD, VIDEO_SETUP_NEW, "partial image recovery setup")
    video_text, _ = _replace_once(video_text, VIDEO_GROUP_OLD, VIDEO_GROUP_NEW, "preserve visual group count")
    video_text, _ = _replace_once(video_text, VIDEO_SCENE_OLD, VIDEO_SCENE_NEW, "generate only missing image groups")
    video_text, _ = _replace_once(video_text, VIDEO_OPENING_OLD, VIDEO_OPENING_NEW, "reuse opening image")
    VIDEO_GENERATOR.write_text(video_text, encoding="utf-8")


def check() -> None:
    task_text = TASK_MANAGER.read_text(encoding="utf-8")
    yt_text = YOUTUBE.read_text(encoding="utf-8")
    video_text = VIDEO_GENERATOR.read_text(encoding="utf-8")
    service_text = SERVICE.read_text(encoding="utf-8")
    task_required = (
        MARKER_TASK,
        "sync_task_snapshot(str(task_id)",
        "before Redis",
    )
    yt_required = (
        MARKER_YT,
        "confirm_or_prepare_partial_recovery(task_id, payload)",
        "recovery_confirmation_message(confirmed_plan",
        "recovery_payload_patch(task_id, payload, manifest_plan)",
        '"regenerate_missing_images"',
        'manifest_action == "rerender_without_paid_media"',
        '"rebuild_untrusted_audio"',
        '"rebuild_missing_audio"',
        '"rebuild_audio_and_missing_images"',
        'getattr(request, "reuse_audio_from", None)',
        'script["_partial_image_recovery"] = dict(partial_meta)',
        'script["_manifest_recovery_policy"] = dict(recovery_policy)',
        'bool(recovery_policy.get("rebuild_audio"))',
    )
    video_required = (
        MARKER_VIDEO,
        "partial_image_recovery",
        "selected_image_count=(0 if partial_image_recovery else len(selected_image_paths))",
        "visual_group_id < len(selected_image_paths)",
        "selected_primary_path if partial_image_recovery",
    )
    service_required = (
        "def sync_task_snapshot(",
        "def build_recovery_plan(",
        "def confirm_or_prepare_partial_recovery(",
        "Nenhuma chamada paga foi feita ainda",
        "_recovery_generate_missing_images_only",
        MARKER_SERVICE,
        'seeded["_partial_image_recovery"]',
    )
    missing = [token for token in task_required if token not in task_text]
    missing.extend(token for token in yt_required if token not in yt_text)
    missing.extend(token for token in video_required if token not in video_text)
    missing.extend(token for token in service_required if token not in service_text)
    if missing:
        raise PatchError(f"production manifest hardening incompleto: {missing}")
    if PAID_GUARD_OLD in yt_text:
        raise PatchError("guard antigo de recuperação paga ainda está presente")
    compile(task_text, str(TASK_MANAGER), "exec")
    compile(yt_text, str(YOUTUBE), "exec")
    compile(video_text, str(VIDEO_GENERATOR), "exec")
    compile(service_text, str(SERVICE), "exec")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO PRODUCTION MANIFEST HARDENING: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
