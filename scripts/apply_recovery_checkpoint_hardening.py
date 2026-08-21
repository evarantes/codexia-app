from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/routers/youtube.py"
START = "# CODEXIA_RECOVERY_CHECKPOINT_V3_START"
END = "# CODEXIA_RECOVERY_CHECKPOINT_V3_END"


BLOCK = r'''

# CODEXIA_RECOVERY_CHECKPOINT_V3_START
# Recovery v3: VideoTask and UnifiedVideo are both authoritative checkpoint
# stores. Retry must inventory both before deciding whether paid stages may run.
def _recovery_json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _recovery_collect_visual_candidates(result_obj: Any) -> List[str]:
    candidates: List[str] = []

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        item = value.strip()
        if item and item not in candidates:
            candidates.append(item)

    def _from_scene_list(values: Any) -> None:
        if not isinstance(values, list):
            return
        for item in values:
            if isinstance(item, str):
                _add(item)
                continue
            if not isinstance(item, dict):
                continue
            for key in (
                "image_path",
                "generated_image_path",
                "source_path",
                "background_image_path",
                "selected_image_path",
                "path",
                "image_url",
                "url",
                "storage_key",
            ):
                _add(item.get(key))

    if not isinstance(result_obj, dict):
        return candidates

    script = result_obj.get("script") if isinstance(result_obj.get("script"), dict) else {}
    for key in ("selected_images", "rendered_images", "images", "custom_image_paths"):
        values = script.get(key)
        if isinstance(values, list):
            for value in values:
                _add(value)

    for key in ("selected_images", "rendered_images", "images", "custom_image_paths"):
        values = result_obj.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    _add(value)
                elif isinstance(value, dict):
                    _from_scene_list([value])

    report = result_obj.get("render_report") if isinstance(result_obj.get("render_report"), dict) else {}
    _from_scene_list(report.get("scene_visuals"))
    visual_plan = report.get("visual_plan") if isinstance(report.get("visual_plan"), dict) else {}
    for key in ("selected_images", "rendered_images", "images"):
        values = visual_plan.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    _add(value)
                elif isinstance(value, dict):
                    _from_scene_list([value])

    storyboard = result_obj.get("storyboard") if isinstance(result_obj.get("storyboard"), dict) else {}
    _from_scene_list(storyboard.get("scenes"))
    return candidates


def _recovery_audio_duration_plausible(duration_sec: Any, target_minutes: Any) -> bool:
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


def _recovery_collect_audio_candidates(result_obj: Any) -> List[str]:
    candidates: List[str] = []

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        item = value.strip()
        if not item:
            return
        ext = os.path.splitext(item.split("?", 1)[0])[1].lower()
        if ext not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
            return
        if item not in candidates:
            candidates.append(item)

    def _walk(value: Any, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_norm = str(key or "").strip().lower()
                if isinstance(child, str) and (
                    "audio" in key_norm
                    or key_norm in {"output_path", "file_path", "path"}
                ):
                    _add(child)
                elif isinstance(child, (dict, list)):
                    _walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    _walk(child, depth + 1)

    if isinstance(result_obj, dict):
        _walk(result_obj)
    return candidates


def _recovery_probe_audio_duration(path_value: Any) -> float:
    try:
        path = str(path_value or "").strip()
        if not path or not os.path.exists(path):
            return 0.0
        probe = probe_media_file(path)
        for key in ("audio_duration", "format_duration", "duration", "video_duration"):
            try:
                value = float(probe.get(key) or 0.0)
            except Exception:
                value = 0.0
            if value > 0.1:
                return value
    except Exception:
        return 0.0
    return 0.0


def _recovery_unified_snapshot(db: Any, task_id: str) -> Dict[str, Any]:
    try:
        uv = (
            db.query(UnifiedVideo)
            .filter(UnifiedVideo.task_id == str(task_id))
            .order_by(UnifiedVideo.updated_at.desc())
            .first()
        )
    except Exception:
        uv = None
    if uv is None:
        return {}

    snapshot = _recovery_json_dict(getattr(uv, "result_json", None))
    script = _recovery_json_dict(getattr(uv, "script_json", None))
    if script:
        snapshot["script"] = script

    storyboard = _recovery_json_dict(getattr(uv, "storyboard_json", None))
    if storyboard:
        snapshot["storyboard"] = storyboard
        scenes = storyboard.get("scenes") if isinstance(storyboard.get("scenes"), list) else []
        if scenes:
            report = snapshot.get("render_report") if isinstance(snapshot.get("render_report"), dict) else {}
            report = dict(report)
            report.setdefault("scene_visuals", scenes)
            snapshot["render_report"] = report

    images_obj = _recovery_json_dict(getattr(uv, "images_json", None))
    image_paths = images_obj.get("paths") if isinstance(images_obj.get("paths"), list) else []
    if image_paths:
        snapshot["selected_images"] = [str(item) for item in image_paths if str(item or "").strip()]

    audio_path = str(getattr(uv, "audio_path", None) or "").strip()
    try:
        audio_duration = float(getattr(uv, "audio_duration_seconds", None) or 0.0)
    except Exception:
        audio_duration = 0.0
    if audio_path:
        checkpoint = snapshot.get("audio_checkpoint") if isinstance(snapshot.get("audio_checkpoint"), dict) else {}
        checkpoint = dict(checkpoint)
        checkpoint.setdefault("output_path", audio_path)
        checkpoint.setdefault("final_audio_path", audio_path)
        checkpoint.setdefault("audio_path", audio_path)
        if audio_duration > 0:
            checkpoint.setdefault("duration_seconds", audio_duration)
            checkpoint.setdefault("final_audio_duration_sec", audio_duration)
        snapshot["audio_checkpoint"] = checkpoint
        snapshot.setdefault("audio_generation", checkpoint)

    snapshot["_unified_recovery_meta"] = {
        "status": str(getattr(uv, "status", None) or ""),
        "current_step": str(getattr(uv, "current_step", None) or ""),
        "progress": int(getattr(uv, "progress", 0) or 0),
        "duration_minutes": int(getattr(uv, "duration_minutes", 0) or 0),
        "has_script_json": bool(script),
        "has_storyboard_json": bool(storyboard),
        "image_count": len(image_paths),
        "audio_path": audio_path or None,
        "audio_duration_seconds": round(audio_duration, 3) if audio_duration > 0 else None,
        "video_path": str(getattr(uv, "video_path", None) or "").strip() or None,
    }
    return snapshot


def _recovery_choose_audio(sources: List[Dict[str, Any]], target_minutes: Any) -> Tuple[str, float, str]:
    try:
        target_sec = max(60.0, float(target_minutes or 0.0) * 60.0)
    except Exception:
        target_sec = 300.0
    best_path = ""
    best_duration = 0.0
    best_source = ""
    best_distance = float("inf")
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict):
            continue
        known_durations: Dict[str, float] = {}
        checkpoint = source.get("audio_checkpoint") if isinstance(source.get("audio_checkpoint"), dict) else {}
        for key in ("output_path", "final_audio_path", "audio_path"):
            path = str(checkpoint.get(key) or "").strip()
            if not path:
                continue
            try:
                duration = float(checkpoint.get("final_audio_duration_sec") or checkpoint.get("duration_seconds") or 0.0)
            except Exception:
                duration = 0.0
            if duration > 0:
                known_durations[path] = duration
        for path in _recovery_collect_audio_candidates(source):
            if not _file_ok(path):
                continue
            duration = float(known_durations.get(path) or 0.0)
            if duration <= 0:
                duration = _recovery_probe_audio_duration(path)
            if not _recovery_audio_duration_plausible(duration, target_minutes):
                continue
            distance = abs(duration - target_sec)
            if distance < best_distance:
                best_path = path
                best_duration = duration
                best_source = "video_task" if source_index == 0 else "unified_video"
                best_distance = distance
    return best_path, best_duration, best_source


def _recovery_choose_script(sources: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict):
            continue
        candidate = source.get("script") if isinstance(source.get("script"), dict) else None
        if _is_valid_seed_script(candidate):
            return dict(candidate), ("video_task" if source_index == 0 else "unified_video")
    return None, ""


def _maybe_enable_render_only_flags(payload: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    payload["force_reuse_assets"] = True
    payload.pop("_recovery_block_paid_regeneration", None)
    payload.pop("_recovery_missing_assets", None)

    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
        if not row:
            return payload
        result_obj = _recovery_json_dict(getattr(row, "result_json", None))
        unified_obj = _recovery_unified_snapshot(db, str(task_id))
        sources = [result_obj, unified_obj]

        seed_script, script_source = _recovery_choose_script(sources)
        script_ok = _is_valid_seed_script(seed_script)
        valid_images: List[str] = []
        visual_source_counts = {"video_task": 0, "unified_video": 0}
        for source_index, source in enumerate(sources):
            source_name = "video_task" if source_index == 0 else "unified_video"
            for candidate in _recovery_collect_visual_candidates(source):
                try:
                    if _selected_images_ok([candidate]) and candidate not in valid_images:
                        valid_images.append(candidate)
                        visual_source_counts[source_name] += 1
                except Exception:
                    continue

        target_minutes = payload.get("duration") or payload.get("duration_minutes") or 0
        if not target_minutes:
            target_minutes = (
                (unified_obj.get("_unified_recovery_meta") or {}).get("duration_minutes")
                if isinstance(unified_obj.get("_unified_recovery_meta"), dict)
                else 0
            ) or 5
        audio_path, audio_duration, audio_source = _recovery_choose_audio(sources, target_minutes)
        images_ok = bool(valid_images) and _selected_images_ok(valid_images)
        audio_ok = bool(audio_path) and _file_ok(audio_path) and _recovery_audio_duration_plausible(audio_duration, target_minutes)
        render_only = bool(script_ok and images_ok and audio_ok)

        if script_ok and isinstance(seed_script, dict):
            seed_script = dict(seed_script)
            if valid_images:
                seed_script["selected_images"] = list(valid_images)
            result_obj["script"] = seed_script
            payload["seeded_script"] = seed_script
        if valid_images:
            result_obj["selected_images"] = list(valid_images)
            payload["selected_images"] = list(valid_images)
        if audio_ok:
            report = result_obj.get("render_report") if isinstance(result_obj.get("render_report"), dict) else {}
            report = dict(report)
            audio_generation = report.get("audio_generation") if isinstance(report.get("audio_generation"), dict) else {}
            audio_generation = dict(audio_generation)
            audio_generation["output_path"] = audio_path
            audio_generation["final_audio_path"] = audio_path
            audio_generation["duration_seconds"] = round(float(audio_duration), 3)
            audio_generation["recovery_validated_duration_sec"] = round(float(audio_duration), 3)
            report["audio_generation"] = audio_generation
            result_obj["render_report"] = report
            result_obj["audio_checkpoint"] = dict(audio_generation)
            payload["reuse_audio_from"] = dict(audio_generation)

        payload["force_render_only"] = bool(render_only)
        missing: List[str] = []
        if not script_ok:
            missing.append("roteiro")
        if not images_ok:
            missing.append("imagens")
        if not audio_ok:
            missing.append("áudio")
        if missing:
            payload["_recovery_block_paid_regeneration"] = True
            payload["_recovery_missing_assets"] = list(missing)

        recovery = result_obj.get("recovery_checkpoint") if isinstance(result_obj.get("recovery_checkpoint"), dict) else {}
        recovery = dict(recovery)
        recovery.update({
            "version": 3,
            "strategy": "highest_valid_checkpoint_v3",
            "script_ok": bool(script_ok),
            "script_source": script_source or None,
            "valid_image_count": len(valid_images),
            "visual_source_counts": visual_source_counts,
            "audio_ok": bool(audio_ok),
            "audio_source": audio_source or None,
            "audio_duration_sec": round(float(audio_duration or 0.0), 3),
            "target_minutes": int(float(target_minutes or 0)),
            "force_render_only": bool(render_only),
            "missing_assets": list(missing),
            "paid_stage_regeneration_blocked": bool(missing),
            "unified_meta": unified_obj.get("_unified_recovery_meta") if isinstance(unified_obj, dict) else {},
        })
        result_obj["recovery_checkpoint"] = recovery
        result_obj["payload"] = dict(payload)
        try:
            row.result_json = json.dumps(result_obj, ensure_ascii=False)
            db.commit()
        except Exception:
            db.rollback()
        return payload
    finally:
        db.close()

# CODEXIA_RECOVERY_CHECKPOINT_V3_END
'''


BLOCK_PAID_RETRY_OLD = '''        payload = _maybe_enable_render_only_flags(payload, task_id)\n        try:\n            VideoRequest(**payload)'''
BLOCK_PAID_RETRY_NEW = '''        payload = _maybe_enable_render_only_flags(payload, task_id)\n        if bool(payload.get("_recovery_block_paid_regeneration")):\n            missing = [str(item) for item in (payload.get("_recovery_missing_assets") or []) if str(item or "").strip()]\n            missing_label = ", ".join(missing) if missing else "ativos necessários"\n            recovery_message = (\n                "Recuperação segura bloqueada antes de novas chamadas pagas: "\n                f"faltam {missing_label}. Os ativos existentes foram preservados. "\n                "Nenhuma nova mídia foi gerada nesta tentativa."\n            )\n            update_task(task_id, message=recovery_message)\n            raise HTTPException(status_code=409, detail=recovery_message)\n        try:\n            VideoRequest(**payload)'''


def _strip_existing(text: str) -> str:
    for start, end in (
        ("# CODEXIA_RECOVERY_CHECKPOINT_V2_START", "# CODEXIA_RECOVERY_CHECKPOINT_V2_END"),
        (START, END),
    ):
        pattern = re.compile(rf"\n?{re.escape(start)}.*?{re.escape(end)}\n?", flags=re.DOTALL)
        text = pattern.sub("\n", text)
    return text.rstrip() + "\n"


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    text = _strip_existing(text)
    if BLOCK_PAID_RETRY_NEW not in text:
        if BLOCK_PAID_RETRY_OLD not in text:
            raise RuntimeError("recovery/paid-retry guard anchor não encontrado")
        text = text.replace(BLOCK_PAID_RETRY_OLD, BLOCK_PAID_RETRY_NEW, 1)
    TARGET.write_text(text.rstrip() + BLOCK + "\n", encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        START,
        '"strategy": "highest_valid_checkpoint_v3"',
        'payload["seeded_script"] = seed_script',
        'payload["selected_images"] = list(valid_images)',
        'payload["reuse_audio_from"] = dict(audio_generation)',
        'payload["force_render_only"] = bool(render_only)',
        'payload["_recovery_block_paid_regeneration"] = True',
        'db.query(UnifiedVideo)',
        'paid_stage_regeneration_blocked',
        'Nenhuma nova mídia foi gerada nesta tentativa.',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise RuntimeError(f"recovery checkpoint v3 ausente: {missing}")
    if "CODEXIA_RECOVERY_CHECKPOINT_V2_START" in text:
        raise RuntimeError("recovery checkpoint v2 antigo ainda presente")
    compile(text, str(TARGET), "exec")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply()
    if args.check:
        check()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")


if __name__ == "__main__":
    main()
