#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "app/services/video_generator.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_MANIFEST_ASSET_PATH_RECOVERY_V1"
CHECKPOINT_MARKER = "CODEXIA_MANIFEST_CHECKPOINT_TRUST_V1"


class PatchError(RuntimeError):
    pass


OLD_SETUP = '''            selected_raw = plan.get("selected_images") or plan.get("images") or []
            if isinstance(selected_raw, list):'''

NEW_SETUP = '''            selected_raw = plan.get("selected_images") or plan.get("images") or []
            # CODEXIA_MANIFEST_ASSET_PATH_RECOVERY_V1
            # Retry payloads can contain absolute paths from a previous API or
            # worker container. Resolve them against this worker's durable
            # manifest before deciding that preserved images are missing.
            manifest_asset_recovery = {}
            force_asset_reuse_requested = bool(
                isinstance(plan, dict)
                and (plan.get("force_reuse_assets") or plan.get("force_render_only"))
            )
            task_id_for_recovery = str(getattr(self, "_codexia_task_id", "") or "").strip()
            if force_asset_reuse_requested and task_id_for_recovery and isinstance(selected_raw, list):
                try:
                    expected_recovery_images = int(
                        (partial_image_meta.get("expected_image_count") if isinstance(partial_image_meta, dict) else 0)
                        or plan.get("expected_image_count")
                        or plan.get("image_count")
                        or len(selected_raw)
                        or len(scenes)
                        or 0
                    )
                except Exception:
                    expected_recovery_images = len(selected_raw)
                try:
                    from app.services.production_manifest import resolve_recovery_image_paths
                    manifest_asset_recovery = resolve_recovery_image_paths(
                        task_id_for_recovery,
                        selected_raw,
                        expected_count=expected_recovery_images,
                    )
                except Exception as recovery_exc:
                    manifest_asset_recovery = {
                        "paths": [],
                        "complete": False,
                        "paid_calls_performed": False,
                        "error": f"{type(recovery_exc).__name__}: {str(recovery_exc)[:180]}",
                    }
                recovered_paths = [
                    str(path)
                    for path in (manifest_asset_recovery.get("paths") or [])
                    if isinstance(path, str) and str(path).strip()
                ]
                if recovered_paths:
                    selected_raw = recovered_paths
                    plan["selected_images"] = list(recovered_paths)
                render_report["manifest_asset_recovery"] = {
                    key: value
                    for key, value in manifest_asset_recovery.items()
                    if key != "paths"
                }
            if isinstance(selected_raw, list):'''

OLD_FAILURE = '''                raise Exception("selected_images fornecidas, mas nenhuma imagem válida foi encontrada no disco para reutilização.")'''

NEW_FAILURE = '''                recovery_details = ""
                if isinstance(manifest_asset_recovery, dict) and manifest_asset_recovery:
                    recovery_details = (
                        f" Referências={int(manifest_asset_recovery.get('selected_reference_count') or 0)},"
                        f" candidatos locais={int(manifest_asset_recovery.get('candidate_count') or 0)},"
                        f" ambiguidades={int(manifest_asset_recovery.get('ambiguous_reference_count') or 0)}."
                    )
                raise Exception(
                    "selected_images fornecidas, mas nenhuma imagem válida foi encontrada no disco local para reutilização."
                    + recovery_details
                    + " Nenhuma chamada paga foi realizada."
                )'''


OLD_CHECKPOINT = '''        audio_path, audio_duration, audio_source = _recovery_choose_audio(sources, target_minutes)
        images_ok = bool(valid_images) and _selected_images_ok(valid_images)
        audio_ok = bool(audio_path) and _file_ok(audio_path) and _recovery_audio_duration_plausible(audio_duration, target_minutes)
        render_only = bool(script_ok and images_ok and audio_ok)'''

NEW_CHECKPOINT = '''        audio_path, audio_duration, audio_source = _recovery_choose_audio(sources, target_minutes)
        # CODEXIA_MANIFEST_CHECKPOINT_TRUST_V1
        # The database can still point to a legacy audio file or stale image
        # paths. The durable manifest is authoritative for cross-container path
        # repair and for narration trust before any paid retry is dispatched.
        try:
            manifest_checkpoint_plan = build_recovery_plan(task_id, payload_override=payload)
        except Exception:
            manifest_checkpoint_plan = {}
        if isinstance(manifest_checkpoint_plan, dict):
            for manifest_image in manifest_checkpoint_plan.get("existing_image_paths") or []:
                try:
                    if _selected_images_ok([manifest_image]) and manifest_image not in valid_images:
                        valid_images.append(manifest_image)
                        visual_source_counts.setdefault("production_manifest", 0)
                        visual_source_counts["production_manifest"] += 1
                except Exception:
                    continue
            if bool(manifest_checkpoint_plan.get("audio_reusable")):
                manifest_audio = str(manifest_checkpoint_plan.get("audio_path") or "").strip()
                if manifest_audio and _file_ok(manifest_audio):
                    audio_path = manifest_audio
                    audio_duration = float(manifest_checkpoint_plan.get("audio_duration_sec") or 0.0)
                    audio_source = "production_manifest"

        images_ok = bool(valid_images) and _selected_images_ok(valid_images)
        audio_ok = bool(audio_path) and _file_ok(audio_path) and _recovery_audio_duration_plausible(audio_duration, target_minutes)
        manifest_action = str(manifest_checkpoint_plan.get("action") or "") if isinstance(manifest_checkpoint_plan, dict) else ""
        if manifest_action in {"rebuild_untrusted_audio", "rebuild_missing_audio", "rebuild_audio_and_missing_images"}:
            audio_path = ""
            audio_duration = 0.0
            audio_source = ""
            audio_ok = False
            payload.pop("reuse_audio_from", None)
        render_only = bool(script_ok and images_ok and audio_ok)'''


def patch_video(text: str) -> str:
    if MARKER not in text:
        count = text.count(OLD_SETUP)
        if count != 1:
            raise PatchError(f"setup de selected_images esperado uma vez; encontrado {count}")
        text = text.replace(OLD_SETUP, NEW_SETUP, 1)
    if NEW_FAILURE not in text:
        count = text.count(OLD_FAILURE)
        if count != 1:
            raise PatchError(f"falha de selected_images esperada uma vez; encontrada {count}")
        text = text.replace(OLD_FAILURE, NEW_FAILURE, 1)
    return text


def patch_youtube(text: str) -> str:
    if CHECKPOINT_MARKER in text:
        return text
    if "CODEXIA_PRODUCTION_MANIFEST_RECOVERY_V1" not in text:
        raise PatchError("production manifest recovery deve ser aplicado antes do checkpoint trust")
    count = text.count(OLD_CHECKPOINT)
    if count != 1:
        raise PatchError(f"checkpoint V3 esperado uma vez; encontrado {count}")
    return text.replace(OLD_CHECKPOINT, NEW_CHECKPOINT, 1)


def apply(*, write: bool) -> int:
    changed = 0
    for path, patcher in ((VIDEO, patch_video), (YOUTUBE, patch_youtube)):
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        if patcher(transformed) != transformed:
            raise PatchError(f"patch não idempotente: {path.name}")
        if transformed != original:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
    print(f"Manifest asset recovery hardening: {changed} arquivo(s) {'aplicados' if write else 'necessários'}")
    return changed


def check() -> None:
    text = patch_video(VIDEO.read_text(encoding="utf-8"))
    youtube = patch_youtube(YOUTUBE.read_text(encoding="utf-8"))
    required = (
        MARKER,
        "resolve_recovery_image_paths(",
        'plan["selected_images"] = list(recovered_paths)',
        'render_report["manifest_asset_recovery"]',
        "Nenhuma chamada paga foi realizada.",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError(f"recuperação de caminhos do manifesto incompleta: {missing}")
    youtube_required = (
        CHECKPOINT_MARKER,
        "manifest_checkpoint_plan = build_recovery_plan(task_id, payload_override=payload)",
        'manifest_checkpoint_plan.get("audio_reusable")',
        'payload.pop("reuse_audio_from", None)',
        '"rebuild_untrusted_audio"',
    )
    youtube_missing = [token for token in youtube_required if token not in youtube]
    if youtube_missing:
        raise PatchError(f"confiança do checkpoint de manifesto incompleta: {youtube_missing}")
    compile(text, str(VIDEO), "exec")
    compile(youtube, str(YOUTUBE), "exec")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        apply(write=bool(args.apply))
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO MANIFEST ASSET RECOVERY: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
