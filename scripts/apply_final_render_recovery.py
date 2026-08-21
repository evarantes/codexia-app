from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/routers/youtube.py"
START = "# CODEXIA_FINAL_RENDER_RECOVERY_V1_START"
END = "# CODEXIA_FINAL_RENDER_RECOVERY_V1_END"


BLOCK = r'''

# CODEXIA_FINAL_RENDER_RECOVERY_V1_START
# A task may fail after VideoGenerator has already written a valid MP4 but before
# the wrapper returns its result. In that case image references can be lost even
# though the final render is still safely persisted in /data/media/videos.
# Recovery must prefer that finished local render over any new paid media call.
def _recovery_final_video_duration_plausible(duration_sec: Any, target_minutes: Any) -> bool:
    try:
        duration = float(duration_sec or 0.0)
    except Exception:
        duration = 0.0
    try:
        target_sec = max(60.0, float(target_minutes or 0.0) * 60.0)
    except Exception:
        target_sec = 300.0
    if duration <= 1.0:
        return False
    return (target_sec * 0.60) <= duration <= (target_sec * 1.80)


def _recovery_final_video_explicit_candidates(result_obj: Any, unified_obj: Any) -> List[str]:
    values: List[str] = []

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        item = value.strip()
        if item and item not in values:
            values.append(item)

    def _walk(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_norm = str(key or "").strip().lower()
                if key_norm in {"file_path", "video_path", "final_video_path", "output_video_path", "video_url"}:
                    _add(child)
                if isinstance(child, (dict, list)):
                    _walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    _walk(child, depth + 1)

    _walk(result_obj)
    _walk(unified_obj)
    meta = unified_obj.get("_unified_recovery_meta") if isinstance(unified_obj, dict) and isinstance(unified_obj.get("_unified_recovery_meta"), dict) else {}
    _add(meta.get("video_path"))
    return values


def _recovery_final_video_abs_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        if os.path.isfile(raw):
            return os.path.abspath(raw)
    except Exception:
        pass
    try:
        from app.config import absolute_path_for_video
        resolved = absolute_path_for_video(raw)
        if resolved and os.path.isfile(resolved):
            return os.path.abspath(resolved)
    except Exception:
        pass
    return ""


def _recovery_final_video_url(path_value: Any) -> str:
    path = str(path_value or "").strip()
    if not path:
        return ""
    try:
        from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX
        abs_output = os.path.abspath(str(VIDEO_OUTPUT_DIR))
        abs_path = os.path.abspath(path)
        if os.path.commonpath([abs_output, abs_path]) == abs_output:
            return f"{VIDEO_URL_PREFIX}/{os.path.basename(abs_path)}"
    except Exception:
        pass
    return ""


def _recovery_final_video_audio_checkpoint_duration(result_obj: Dict[str, Any], unified_obj: Dict[str, Any]) -> float:
    for source in (result_obj, unified_obj):
        if not isinstance(source, dict):
            continue
        recovery = source.get("recovery_checkpoint") if isinstance(source.get("recovery_checkpoint"), dict) else {}
        for key in ("audio_duration_sec", "audio_duration_seconds"):
            try:
                value = float(recovery.get(key) or 0.0)
            except Exception:
                value = 0.0
            if value > 0:
                return value
        checkpoint = source.get("audio_checkpoint") if isinstance(source.get("audio_checkpoint"), dict) else {}
        for key in ("final_audio_duration_sec", "duration_seconds", "audio_duration_seconds"):
            try:
                value = float(checkpoint.get(key) or 0.0)
            except Exception:
                value = 0.0
            if value > 0:
                return value
    meta = unified_obj.get("_unified_recovery_meta") if isinstance(unified_obj.get("_unified_recovery_meta"), dict) else {}
    try:
        return float(meta.get("audio_duration_seconds") or 0.0)
    except Exception:
        return 0.0


def _recovery_final_video_claimed_paths(db: Any, task_id: str) -> set:
    claimed = set()
    try:
        rows = db.query(UnifiedVideo).filter(UnifiedVideo.task_id != str(task_id)).all()
    except Exception:
        rows = []
    for uv in rows:
        for value in (getattr(uv, "video_path", None), getattr(uv, "video_url", None)):
            resolved = _recovery_final_video_abs_path(value)
            if resolved:
                claimed.add(resolved)
    return claimed


def _recovery_probe_final_video(path: str, *, target_minutes: Any, audio_duration_sec: float = 0.0) -> Dict[str, Any]:
    try:
        probe = probe_media_file(path)
    except Exception as exc:
        return {"ok": False, "path": path, "error": f"probe_failed:{type(exc).__name__}"}
    if not isinstance(probe, dict) or not probe.get("ok"):
        return {"ok": False, "path": path, "probe": probe, "error": str((probe or {}).get("error") or "invalid_media")}
    try:
        if not media_durations_match(probe):
            return {"ok": False, "path": path, "probe": probe, "error": "audio_video_duration_mismatch"}
    except Exception:
        return {"ok": False, "path": path, "probe": probe, "error": "audio_video_duration_check_failed"}
    duration = max(
        float(probe.get("video_duration") or 0.0),
        float(probe.get("audio_duration") or 0.0),
        float(probe.get("format_duration") or 0.0),
    )
    if not _recovery_final_video_duration_plausible(duration, target_minutes):
        return {"ok": False, "path": path, "probe": probe, "duration_sec": duration, "error": "duration_outside_target"}
    audio_delta = 0.0
    if float(audio_duration_sec or 0.0) > 0:
        audio_delta = abs(duration - float(audio_duration_sec))
        tolerance = max(5.0, float(audio_duration_sec) * 0.015)
        if audio_delta > tolerance:
            return {
                "ok": False,
                "path": path,
                "probe": probe,
                "duration_sec": duration,
                "audio_checkpoint_duration_sec": float(audio_duration_sec),
                "audio_delta_sec": audio_delta,
                "error": "render_does_not_match_audio_checkpoint",
            }
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "probe": probe,
        "duration_sec": duration,
        "audio_delta_sec": audio_delta,
        "file_size_bytes": int(probe.get("file_size_bytes") or 0),
    }


def _recovery_choose_existing_final_video(
    db: Any,
    row: Any,
    result_obj: Dict[str, Any],
    unified_obj: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        from app.config import VIDEO_OUTPUT_DIR
    except Exception:
        return {"found": False, "reason": "video_output_dir_unavailable", "candidates": []}

    target_minutes = payload.get("duration") or payload.get("duration_minutes") or 0
    if not target_minutes:
        meta = unified_obj.get("_unified_recovery_meta") if isinstance(unified_obj.get("_unified_recovery_meta"), dict) else {}
        target_minutes = meta.get("duration_minutes") or 5
    audio_duration = _recovery_final_video_audio_checkpoint_duration(result_obj, unified_obj)
    claimed = _recovery_final_video_claimed_paths(db, str(getattr(row, "id", None) or ""))

    explicit_paths: List[str] = []
    for raw in _recovery_final_video_explicit_candidates(result_obj, unified_obj):
        resolved = _recovery_final_video_abs_path(raw)
        if resolved and resolved not in explicit_paths:
            explicit_paths.append(resolved)

    diagnostics: List[Dict[str, Any]] = []
    for path in explicit_paths:
        if path in claimed:
            continue
        report = _recovery_probe_final_video(path, target_minutes=target_minutes, audio_duration_sec=audio_duration)
        report["source"] = "persisted_reference"
        diagnostics.append(report)
        if report.get("ok"):
            return {"found": True, "candidate": report, "source": "persisted_reference", "candidates": diagnostics}

    try:
        scanned = sorted(
            glob.glob(os.path.join(str(VIDEO_OUTPUT_DIR), "*.mp4")),
            key=lambda item: os.path.getmtime(item) if os.path.exists(item) else 0.0,
            reverse=True,
        )
    except Exception:
        scanned = []

    try:
        created_at = getattr(row, "created_at", None)
        created_ts = float(created_at.timestamp()) if created_at is not None else 0.0
    except Exception:
        created_ts = 0.0

    valid_scanned: List[Dict[str, Any]] = []
    for path in scanned[:120]:
        abs_path = os.path.abspath(path)
        if abs_path in claimed or abs_path in explicit_paths:
            continue
        try:
            mtime = float(os.path.getmtime(abs_path))
        except Exception:
            mtime = 0.0
        if created_ts > 0 and mtime > 0 and mtime < (created_ts - 10 * 60):
            continue
        report = _recovery_probe_final_video(abs_path, target_minutes=target_minutes, audio_duration_sec=audio_duration)
        report["source"] = "video_output_scan"
        report["mtime"] = mtime
        diagnostics.append(report)
        if report.get("ok"):
            valid_scanned.append(report)

    if not valid_scanned:
        return {"found": False, "reason": "no_valid_final_render", "candidates": diagnostics[-20:]}

    try:
        target_sec = max(60.0, float(target_minutes or 0.0) * 60.0)
    except Exception:
        target_sec = 300.0
    valid_scanned.sort(
        key=lambda item: (
            float(item.get("audio_delta_sec") or 0.0) if audio_duration > 0 else abs(float(item.get("duration_sec") or 0.0) - target_sec),
            abs(float(item.get("duration_sec") or 0.0) - target_sec),
            -float(item.get("mtime") or 0.0),
        )
    )
    chosen = valid_scanned[0]
    if len(valid_scanned) > 1:
        first_delta = float(chosen.get("audio_delta_sec") or 0.0) if audio_duration > 0 else abs(float(chosen.get("duration_sec") or 0.0) - target_sec)
        second = valid_scanned[1]
        second_delta = float(second.get("audio_delta_sec") or 0.0) if audio_duration > 0 else abs(float(second.get("duration_sec") or 0.0) - target_sec)
        # Avoid associating a random MP4 when two different renders look equally
        # compatible. A later retry can expose the candidates for manual audit.
        if abs(first_delta - second_delta) < 0.20 and abs(float(chosen.get("mtime") or 0.0) - float(second.get("mtime") or 0.0)) < 120.0:
            return {
                "found": False,
                "reason": "ambiguous_final_render_candidates",
                "valid_candidate_count": len(valid_scanned),
                "candidates": valid_scanned[:8],
            }
    return {"found": True, "candidate": chosen, "source": "video_output_scan", "candidates": diagnostics[-20:]}


def _recovery_extract_review_frames(video_path: str, task_id: str, frame_count: int, duration_sec: float) -> Tuple[List[str], str]:
    try:
        from app.config import IMAGES_OUTPUT_DIR
        import subprocess
    except Exception as exc:
        return [], f"frame_dependencies_unavailable:{type(exc).__name__}"

    count = max(1, min(64, int(frame_count or 1)))
    safe_task = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(task_id or "task"))[:48]
    os.makedirs(str(IMAGES_OUTPUT_DIR), exist_ok=True)
    prefix = f"recovered_render_{safe_task}_"
    existing = sorted(glob.glob(os.path.join(str(IMAGES_OUTPUT_DIR), prefix + "*.jpg")))
    existing = [p for p in existing if os.path.isfile(p) and os.path.getsize(p) >= 2000]
    if len(existing) >= count:
        return existing[:count], "reused_existing_frames"

    pattern = os.path.join(str(IMAGES_OUTPUT_DIR), prefix + "%03d.jpg")
    try:
        fps_value = float(count) / max(1.0, float(duration_sec or 0.0))
    except Exception:
        fps_value = 0.01
    fps_value = max(0.0005, min(1.0, fps_value))
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video_path),
                "-vf", f"fps={fps_value:.8f}",
                "-frames:v", str(count),
                "-q:v", "3",
                "-threads", "1",
                pattern,
            ],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    except Exception as exc:
        return [], f"frame_extraction_exception:{type(exc).__name__}:{str(exc)[:160]}"
    frames = sorted(glob.glob(os.path.join(str(IMAGES_OUTPUT_DIR), prefix + "*.jpg")))
    frames = [p for p in frames if os.path.isfile(p) and os.path.getsize(p) >= 2000]
    if completed.returncode != 0 or len(frames) < count:
        error = str(completed.stderr or "").strip().replace("\n", " ")[:240]
        return frames, f"frame_extraction_incomplete:{len(frames)}/{count}:{error}"
    return frames[:count], "generated_from_final_render"


def _recovery_try_promote_final_render(payload: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
        if row is None:
            return None
        result_obj = _recovery_json_dict(getattr(row, "result_json", None))
        unified_obj = _recovery_unified_snapshot(db, str(task_id))
        uv = (
            db.query(UnifiedVideo)
            .filter(UnifiedVideo.task_id == str(task_id))
            .order_by(UnifiedVideo.updated_at.desc())
            .first()
        )
        if uv is None:
            return None

        choice = _recovery_choose_existing_final_video(db, row, result_obj, unified_obj, payload)
        if not choice.get("found"):
            recovery = result_obj.get("recovery_final_render") if isinstance(result_obj.get("recovery_final_render"), dict) else {}
            recovery = dict(recovery)
            recovery.update({
                "version": 1,
                "found": False,
                "reason": choice.get("reason") or "not_found",
                "valid_candidate_count": int(choice.get("valid_candidate_count") or 0),
                "checked_without_paid_calls": True,
            })
            result_obj["recovery_final_render"] = recovery
            try:
                row.result_json = json.dumps(result_obj, ensure_ascii=False)
                db.commit()
            except Exception:
                db.rollback()
            if choice.get("reason") == "ambiguous_final_render_candidates":
                message = (
                    "Recuperação segura encontrou mais de um MP4 final compatível e não escolheu automaticamente. "
                    "Nenhuma nova mídia foi gerada."
                )
                update_task(task_id, message=message)
                return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}
            return None

        candidate = choice.get("candidate") if isinstance(choice.get("candidate"), dict) else {}
        video_path = str(candidate.get("path") or "").strip()
        video_url = _recovery_final_video_url(video_path)
        duration_sec = float(candidate.get("duration_sec") or 0.0)
        requested_frames = max(1, min(64, int(getattr(uv, "image_count", 1) or 1)))
        frames, frame_source = _recovery_extract_review_frames(video_path, task_id, requested_frames, duration_sec)
        if len(frames) < requested_frames:
            message = (
                "MP4 final válido foi encontrado, mas a recuperação local dos quadros de auditoria falhou. "
                f"Quadros recuperados: {len(frames)}/{requested_frames}. Nenhuma chamada paga foi feita."
            )
            result_obj["recovery_final_render"] = {
                "version": 1,
                "found": True,
                "promoted": False,
                "video_path": video_path,
                "video_url": video_url,
                "duration_sec": round(duration_sec, 3),
                "frame_source": frame_source,
                "frame_count": len(frames),
                "required_frame_count": requested_frames,
                "checked_without_paid_calls": True,
            }
            row.result_json = json.dumps(result_obj, ensure_ascii=False)
            db.commit()
            update_task(task_id, message=message)
            return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}

        recovery_meta = {
            "version": 1,
            "found": True,
            "promoted": True,
            "source": choice.get("source") or candidate.get("source") or "unknown",
            "video_path": video_path,
            "video_url": video_url,
            "duration_sec": round(duration_sec, 3),
            "file_size_bytes": int(candidate.get("file_size_bytes") or 0),
            "audio_delta_sec": round(float(candidate.get("audio_delta_sec") or 0.0), 3),
            "frame_source": frame_source,
            "frame_count": len(frames),
            "required_frame_count": requested_frames,
            "checked_without_paid_calls": True,
        }
        result_obj["file_path"] = video_path
        result_obj["video_path"] = video_path
        result_obj["video_url"] = video_url
        result_obj["selected_images"] = list(frames)
        result_obj["images"] = list(frames)
        result_obj["recovery_final_render"] = recovery_meta
        payload["selected_images"] = list(frames)
        payload["_recovery_block_paid_regeneration"] = False
        payload["_recovery_missing_assets"] = []
        result_obj["payload"] = dict(payload)

        uv.video_path = video_path
        uv.video_url = video_url or getattr(uv, "video_url", None)
        uv.video_size_bytes = int(candidate.get("file_size_bytes") or 0) or None
        uv.video_duration_seconds = duration_sec or None
        uv.images_json = json.dumps({
            "paths": list(frames),
            "source": "recovered_final_render_frames",
            "original_source_images_missing": True,
        }, ensure_ascii=False)
        uv.last_message = "MP4 final recuperado localmente; validando para revisão."
        row.result_json = json.dumps(result_obj, ensure_ascii=False)
        db.commit()

        service = unified_video_pipeline() if callable(unified_video_pipeline) else None
        if service is None:
            message = "MP4 final recuperado, mas o UnifiedVideoPipeline não está disponível para validar a revisão."
            update_task(task_id, message=message)
            return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}
        validation = service.validate_before_awaiting_review(
            db,
            str(task_id),
            probe_local_paths=True,
            probe_http=False,
        )
        if not validation.ok:
            message = (
                "MP4 final recuperado sem novas chamadas pagas, mas a validação física ainda bloqueou em "
                f"{validation.first_failed or 'unknown'}."
            )
            result_obj["recovery_final_render"]["validation_ok"] = False
            result_obj["recovery_final_render"]["validation_first_failed"] = validation.first_failed
            row.result_json = json.dumps(result_obj, ensure_ascii=False)
            db.commit()
            update_task(task_id, message=message)
            return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}

        validation2, transitioned = service.transition_to_awaiting_review_if_valid(
            db,
            str(task_id),
            probe_local_paths=True,
            probe_http=False,
        )
        if not validation2.ok or transitioned is None:
            message = "MP4 final validado, mas não foi possível promover a produção para Aguardando Revisão."
            update_task(task_id, message=message)
            return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}

        result_obj["recovery_final_render"]["validation_ok"] = True
        result_obj["unified_pipeline"] = {
            "validation_ok": True,
            "validation_checks": validation2.checks,
            "recovered_final_render": True,
        }
        success_message = (
            "Render final já existente recuperado e validado sem novas chamadas pagas. "
            "Vídeo disponível para revisão."
        )
        update_task(
            task_id,
            status="awaiting_review",
            progress=100,
            message=success_message,
            result=result_obj,
        )
        return {
            "recovered": True,
            "status": "awaiting_review",
            "task_id": str(task_id),
            "video_url": video_url,
            "message": success_message,
            "recovery_final_render": recovery_meta,
        }
    finally:
        db.close()

# CODEXIA_FINAL_RENDER_RECOVERY_V1_END
'''


RETRY_OLD = '''        payload = _maybe_enable_render_only_flags(payload, task_id)\n        if bool(payload.get("_recovery_block_paid_regeneration")):'''
RETRY_NEW = '''        payload = _maybe_enable_render_only_flags(payload, task_id)\n        final_render_recovery = _recovery_try_promote_final_render(payload, task_id)\n        if isinstance(final_render_recovery, dict) and final_render_recovery.get("recovered"):\n            return final_render_recovery\n        if isinstance(final_render_recovery, dict) and final_render_recovery.get("blocked"):\n            raise HTTPException(status_code=409, detail=str(final_render_recovery.get("message") or "Recuperação bloqueada."))\n        if bool(payload.get("_recovery_block_paid_regeneration")):'''


class PatchError(RuntimeError):
    pass


def _strip_existing(text: str) -> str:
    pattern = re.compile(rf"\n?{re.escape(START)}.*?{re.escape(END)}\n?", flags=re.DOTALL)
    return pattern.sub("\n", text).rstrip() + "\n"


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    text = _strip_existing(text)
    if "CODEXIA_RECOVERY_CHECKPOINT_V3_START" not in text:
        raise PatchError("recovery checkpoint v3 deve ser aplicado antes do final-render recovery")
    if RETRY_NEW not in text:
        if RETRY_OLD not in text:
            raise PatchError("anchor do retry v3 não encontrado")
        text = text.replace(RETRY_OLD, RETRY_NEW, 1)
    TARGET.write_text(text.rstrip() + BLOCK + "\n", encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        START,
        "_recovery_try_promote_final_render(payload, task_id)",
        "_recovery_choose_existing_final_video",
        "probe_media_file(path)",
        "media_durations_match(probe)",
        "recovered_final_render_frames",
        "transition_to_awaiting_review_if_valid",
        "Render final já existente recuperado e validado sem novas chamadas pagas.",
    )
    missing = [item for item in required if item not in text]
    if missing:
        raise PatchError(f"final render recovery incompleto: {missing}")
    if text.count(START) != 1 or text.count(END) != 1:
        raise PatchError("marcadores final render recovery duplicados")
    compile(text, str(TARGET), "exec")


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
        print(f"ERRO FINAL RENDER RECOVERY: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
