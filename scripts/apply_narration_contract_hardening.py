#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "app/services/video_generator.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
WORKER = ROOT / "app/worker.py"
MANIFEST = ROOT / "app/services/production_manifest.py"

MARKER_VIDEO = "CODEXIA_NARRATION_CONTRACT_PROTECTED_CLOSING_V1"
MARKER_TRACK = "CODEXIA_IMMEDIATE_IMAGE_MANIFEST_V1"
MARKER_RENDER = "CODEXIA_POST_RENDER_DURATION_GATE_V1"
MARKER_YOUTUBE = "CODEXIA_NARRATION_CONTRACT_RUNTIME_V1"
MARKER_WORKER = "CODEXIA_NARRATION_CONTRACT_WORKER_V1"
MARKER_MANIFEST = "CODEXIA_IMMEDIATE_ARTIFACT_MANIFEST_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def patch_video(text: str) -> str:
    text = _replace_once(
        text,
        '''    def _default_closing_text(self, channel_name: str) -> str:\n        safe_channel = str(channel_name or "").strip() or "Herdeiros das Promessas"\n        return (\n            f"Continue conosco. Inscreva-se no canal {safe_channel} "\n            "e acompanhe as próximas mensagens de fé."\n        )''',
        '''    def _default_closing_text(self, channel_name: str) -> str:\n        # CODEXIA_NARRATION_CONTRACT_PROTECTED_CLOSING_V1\n        safe_channel = str(channel_name or "").strip() or "Herdeiros das Promessas"\n        return (\n            f"Se esta mensagem falou com você, inscreva-se no canal {safe_channel}, "\n            "ative o sininho para receber as próximas mensagens e compartilhe este vídeo "\n            "com alguém que precisa ouvi-lo."\n        )''',
        "complete protected CTA",
    )
    text = _replace_once(
        text,
        '        body_text = " ".join(part for part in [story_text, reflection_text] if part).strip()',
        '        # CODEXIA_NARRATION_CONTRACT_PROTECTED_CLOSING_V1\n        # Reflection is protected closing material, never part of the condensable body.\n        body_text = story_text',
        "reflection outside condensable body",
    )
    text = _replace_once(
        text,
        '            total_est = intro_opening_hold_sec + opening_est + body_est + closing_est + pause_duration_sec',
        '            total_est = intro_opening_hold_sec + opening_est + body_est + reflection_est + closing_est + pause_duration_sec',
        "planning total includes reflection",
    )
    text = _replace_once(
        text,
        '            target_body_max_sec = max(8.0, planning_max_total_sec - opening_est - closing_est)',
        '            target_body_max_sec = max(8.0, planning_max_total_sec - opening_est - reflection_est - closing_est)',
        "body budget reserves reflection and CTA",
    )
    text = _replace_once(
        text,
        '        full_text_parts = [opening_text.strip(), body_text.strip(), closing_text.strip()]',
        '        full_text_parts = [opening_text.strip(), body_text.strip(), reflection_text.strip(), closing_text.strip()]',
        "full narration preserves reflection",
    )
    text = _replace_once(
        text,
        '        total_est = intro_opening_hold_sec + opening_est + body_est + closing_est + pause_duration_sec',
        '        total_est = intro_opening_hold_sec + opening_est + body_est + reflection_est + closing_est + pause_duration_sec',
        "final estimate includes reflection",
    )
    text = _replace_once(
        text,
        '''                    str(planning_meta.get("opening_text") or "").strip(),\n                    str(planning_meta.get("body_text") or "").strip(),''',
        '''                    str(planning_meta.get("opening_text") or "").strip(),\n                    str(planning_meta.get("body_text") or "").strip(),\n                    str(planning_meta.get("reflection_text") or "").strip(),''',
        "main narration includes protected reflection",
    )
    text = _replace_once(
        text,
        '                target_body_max_sec = max(8.0, (real_audio_target_max_sec or max_requested_duration or actual_total_audio_dur) - initial_opening_silence_sec - opening_duration_est - closing_duration_est)',
        '                target_body_max_sec = max(8.0, (real_audio_target_max_sec or max_requested_duration or actual_total_audio_dur) - initial_opening_silence_sec - opening_duration_est - float(planning_meta.get("reflection_duration_est_sec") or 0.0) - closing_duration_est)',
        "real audio budget reserves reflection",
    )
    text = _replace_once(
        text,
        '                planning_meta["word_count"] = self._count_words(" ".join([current_opening, new_body_text, current_closing]).strip())',
        '                planning_meta["word_count"] = self._count_words(" ".join([current_opening, new_body_text, str(planning_meta.get("reflection_text") or "").strip(), current_closing]).strip())',
        "replan word count includes reflection",
    )
    text = _replace_once(
        text,
        '                planning_meta["full_text"] = " ".join([current_opening, new_body_text, current_closing]).strip()',
        '                planning_meta["full_text"] = " ".join([current_opening, new_body_text, str(planning_meta.get("reflection_text") or "").strip(), current_closing]).strip()',
        "replan full text includes reflection",
    )
    text = _replace_once(
        text,
        '''                    + float(planning_meta.get("body_duration_est_sec") or 0.0)\n                    + float(planning_meta.get("closing_duration_est_sec") or 0.0)''',
        '''                    + float(planning_meta.get("body_duration_est_sec") or 0.0)\n                    + float(planning_meta.get("reflection_duration_est_sec") or 0.0)\n                    + float(planning_meta.get("closing_duration_est_sec") or 0.0)''',
        "replan estimate includes reflection",
    )
    text = _replace_once(
        text,
        '''        def _track_image_path(p: str):\n            try:\n                if not p or not isinstance(p, str):\n                    return''',
        '''        def _track_image_path(p: str):\n            try:\n                if not p or not isinstance(p, str):\n                    return\n                # CODEXIA_IMMEDIATE_IMAGE_MANIFEST_V1\n                task_id = str(getattr(self, "_codexia_task_id", "") or "").strip()\n                if task_id and os.path.isfile(p):\n                    try:\n                        from app.services.production_manifest import record_artifact\n                        record_artifact(task_id, p, kind="image", source="renderer_immediate")\n                    except Exception:\n                        pass''',
        "immediate image persistence",
    )
    text = _replace_once(
        text,
        '''            output_path = self._ensure_playable_mp4(output_path)\n            try:\n                self._dbg_event("H1", "_ensure_playable_mp4 done (narrated)", {''',
        '''            output_path = self._ensure_playable_mp4(output_path)\n            # CODEXIA_POST_RENDER_DURATION_GATE_V1\n            task_id = str(getattr(self, "_codexia_task_id", "") or "").strip()\n            if task_id:\n                try:\n                    from app.services.production_manifest import record_artifact\n                    record_artifact(task_id, output_path, kind="video", source="render_immediate")\n                except Exception:\n                    pass\n            rendered_duration = float(self._measure_rendered_video_duration_seconds(output_path) or 0.0)\n            expected_render_duration = float(target_video_duration or actual_total_audio_dur or 0.0)\n            render_tolerance = duration_sync_tolerance_seconds(expected_render_duration) if expected_render_duration > 0 else 0.0\n            if expected_render_duration > 0 and rendered_duration + render_tolerance < expected_render_duration:\n                raise Exception(\n                    "Render final truncado antes da conclusão/CTA: "\n                    f"arquivo={rendered_duration:.2f}s, esperado={expected_render_duration:.2f}s, "\n                    f"tolerância={render_tolerance:.2f}s. O MP4 foi preservado para diagnóstico, mas não será aprovado."\n                )\n            try:\n                self._dbg_event("H1", "_ensure_playable_mp4 done (narrated)", {''',
        "post-render duration gate",
    )
    return text


def patch_youtube(text: str) -> str:
    text = _replace_once(
        text,
        '''def process_video_generation(request: VideoRequest, task_id):\n    # Lazy import VideoGenerator (moviepy/PIL/numpy) para reduzir memória no startup\n    from app.services.video_generator import VideoGenerator''',
        '''def process_video_generation(request: VideoRequest, task_id):\n    # Lazy import VideoGenerator (moviepy/PIL/numpy) para reduzir memória no startup\n    from app.services.video_generator import VideoGenerator\n    # CODEXIA_NARRATION_CONTRACT_RUNTIME_V1\n    from app.services.narration_contract_guard import install_narration_contract_guard\n    install_narration_contract_guard(VideoGenerator)''',
        "install guard in API/subprocess path",
    )
    text = _replace_once(
        text,
        '''        video_service = VideoGenerator(ai_service=ai_service)\n        yt_service = YouTubeService()''',
        '''        video_service = VideoGenerator(ai_service=ai_service)\n        video_service._codexia_task_id = str(task_id)\n        yt_service = YouTubeService()''',
        "propagate task id to renderer",
    )
    return text


def patch_worker(text: str) -> str:
    text = _replace_once(
        text,
        'from app.services.canonical_caption_source import install_canonical_caption_source_patch',
        'from app.services.canonical_caption_source import install_canonical_caption_source_patch\nfrom app.services.narration_contract_guard import install_narration_contract_guard',
        "worker guard import",
    )
    text = _replace_once(
        text,
        '''install_canonical_caption_source_patch(video_generator_cls)\n\n# Contrato de startup:''',
        '''install_canonical_caption_source_patch(video_generator_cls)\n\n# CODEXIA_NARRATION_CONTRACT_WORKER_V1\ninstall_narration_contract_guard(video_generator_cls)\n\n# Contrato de startup:''',
        "worker guard install",
    )
    text = _replace_once(
        text,
        '''if int(getattr(video_generator_cls, "_codexia_caption_integrity_version", 0) or 0) < 4:\n    raise RuntimeError("CaptionIntegritySelfHeal desatualizado: esperado v4.")''',
        '''if int(getattr(video_generator_cls, "_codexia_caption_integrity_version", 0) or 0) < 4:\n    raise RuntimeError("CaptionIntegritySelfHeal desatualizado: esperado v4.")\nif not bool(getattr(video_generator_cls, "_codexia_narration_contract_guard_v1", False)):\n    raise RuntimeError("NarrationContractGuard ausente: worker recusou iniciar.")''',
        "worker startup contract",
    )
    return text


def patch_manifest(text: str) -> str:
    text = _replace_once(
        text,
        'def _probe_duration(path: str) -> float:',
        '''def record_artifact(task_id: Any, path: str, *, kind: str, source: str = "runtime") -> Dict[str, Any]:\n    """Persiste um ativo imediatamente, antes de limpeza temporária ou crash."""\n    # CODEXIA_IMMEDIATE_ARTIFACT_MANIFEST_V1\n    task_key = _safe_task_id(task_id)\n    kind_norm = str(kind or "").strip().lower()\n    if not task_key or kind_norm not in {"image", "audio", "video"}:\n        return {}\n    resolved = _resolve_existing_path(path, kind_norm)\n    if not resolved:\n        raw = os.path.abspath(str(path or "")) if str(path or "").strip() else ""\n        if raw and os.path.isfile(raw):\n            resolved = raw\n    if not resolved or not os.path.isfile(resolved):\n        return {}\n    with _LOCK:\n        mpath = manifest_path(task_key)\n        existing = _read_json(mpath)\n        now_epoch = time.time()\n        if not existing:\n            existing = {\n                "schema_version": _SCHEMA_VERSION,\n                "task_id": task_key,\n                "created_at": _utc_iso(),\n                "manifest_created_at": _utc_iso(),\n                "scan_cursor_epoch": now_epoch,\n                "artifacts": [],\n                "checkpoints": [],\n            }\n        entry = _artifact_entry(task_key, resolved, kind_norm, str(source or "runtime"))\n        existing["artifacts"] = _merge_artifacts(existing.get("artifacts") or [], [entry])\n        existing["updated_at"] = _utc_iso()\n        existing["scan_cursor_epoch"] = now_epoch\n        _atomic_write_json(mpath, existing)\n        return dict(entry)\n\n\ndef _probe_duration(path: str) -> float:''',
        "immediate artifact API",
    )
    text = _replace_once(
        text,
        '        if audio_duration > 0 and abs(duration - audio_duration) > max(4.0, audio_duration * 0.08):',
        '        # Reject partial renders: 8% accepted tens of missing seconds.\n        if audio_duration > 0 and abs(duration - audio_duration) > max(4.0, audio_duration * 0.015):',
        "strict render/audio recovery tolerance",
    )
    return text


def apply(write: bool) -> int:
    changed = 0
    for path, patcher in ((VIDEO, patch_video), (YOUTUBE, patch_youtube), (WORKER, patch_worker), (MANIFEST, patch_manifest)):
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        if patcher(transformed) != transformed:
            raise PatchError(f"{path.name}: patch não idempotente")
        if transformed != original:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
    print(f"Narration contract hardening: {changed} arquivo(s) {'aplicados' if write else 'necessários'}")
    return changed


def check_markers() -> None:
    requirements = {
        VIDEO: (MARKER_VIDEO, MARKER_TRACK, MARKER_RENDER),
        YOUTUBE: (MARKER_YOUTUBE, '_codexia_task_id = str(task_id)'),
        WORKER: (MARKER_WORKER, 'NarrationContractGuard ausente'),
        MANIFEST: (MARKER_MANIFEST, 'def record_artifact(', 'audio_duration * 0.015'),
    }
    for path, needles in requirements.items():
        text = path.read_text(encoding="utf-8")
        missing = [needle for needle in needles if needle not in text]
        if missing:
            raise PatchError(f"{path.name}: contratos ausentes: {missing}")


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
            check_markers()
    except PatchError as exc:
        print(f"ERRO NARRATION CONTRACT: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
