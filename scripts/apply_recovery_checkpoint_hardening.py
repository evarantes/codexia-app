from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/routers/youtube.py"
START = "# CODEXIA_RECOVERY_CHECKPOINT_V2_START"
END = "# CODEXIA_RECOVERY_CHECKPOINT_V2_END"


BLOCK = r'''

# CODEXIA_RECOVERY_CHECKPOINT_V2_START
# Highest-checkpoint recovery: retry must normalize every persisted checkpoint
# before deciding whether paid stages need to run again.
def _recovery_collect_visual_candidates(result_obj: Any) -> List[str]:
    candidates: List[str] = []

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        item = value.strip()
        if item and item not in candidates:
            candidates.append(item)

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
                _add(value)

    report = result_obj.get("render_report") if isinstance(result_obj.get("render_report"), dict) else {}
    scene_visuals = report.get("scene_visuals") if isinstance(report.get("scene_visuals"), list) else []
    for item in scene_visuals:
        if not isinstance(item, dict):
            continue
        for key in (
            "image_path",
            "generated_image_path",
            "source_path",
            "background_image_path",
            "selected_image_path",
        ):
            _add(item.get(key))

    visual_plan = report.get("visual_plan") if isinstance(report.get("visual_plan"), dict) else {}
    for key in ("selected_images", "rendered_images", "images"):
        values = visual_plan.get(key)
        if isinstance(values, list):
            for value in values:
                _add(value)
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
    # Editorial review may legitimately stretch/condense the requested duration,
    # but a 55-second checkpoint can never satisfy a 10-minute production.
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
        if depth > 6:
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


def _recovery_choose_audio(result_obj: Any, target_minutes: Any) -> Tuple[str, float]:
    best_path = ""
    best_duration = 0.0
    try:
        target_sec = max(60.0, float(target_minutes or 0.0) * 60.0)
    except Exception:
        target_sec = 300.0
    best_distance = float("inf")
    for path in _recovery_collect_audio_candidates(result_obj):
        if not _file_ok(path):
            continue
        duration = _recovery_probe_audio_duration(path)
        if not _recovery_audio_duration_plausible(duration, target_minutes):
            continue
        distance = abs(duration - target_sec)
        if distance < best_distance:
            best_path = path
            best_duration = duration
            best_distance = distance
    return best_path, best_duration


def _maybe_enable_render_only_flags(payload: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    """Normalize persisted checkpoints and resume from the highest safe stage.

    The previous implementation only inspected ``script.selected_images`` and
    ``render_report.audio_generation.output_path``. A task that had reached
    stage_6 could therefore fall back to stage_2 after deploy/retry even though
    its images/audio were still present under scene_visuals or another audio
    checkpoint. This version consolidates those representations first.
    """
    if not isinstance(payload, dict):
        return payload
    payload.setdefault("force_reuse_assets", True)

    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
        if not row or not getattr(row, "result_json", None):
            return payload
        try:
            result_obj = json.loads(getattr(row, "result_json", "") or "{}")
        except Exception:
            result_obj = {}
        if not isinstance(result_obj, dict):
            return payload

        seed_script = result_obj.get("script") if isinstance(result_obj.get("script"), dict) else None
        script_ok = _is_valid_seed_script(seed_script)

        visual_candidates = _recovery_collect_visual_candidates(result_obj)
        valid_images: List[str] = []
        for candidate in visual_candidates:
            try:
                if _selected_images_ok([candidate]) and candidate not in valid_images:
                    valid_images.append(candidate)
            except Exception:
                continue

        target_minutes = payload.get("duration") or 5
        audio_path, audio_duration = _recovery_choose_audio(result_obj, target_minutes)

        # Normalize the result into the exact canonical fields consumed by the
        # worker's existing render-only branch. No provider call is made here.
        changed = False
        if script_ok and isinstance(seed_script, dict) and valid_images:
            current_images = seed_script.get("selected_images") if isinstance(seed_script.get("selected_images"), list) else []
            if current_images != valid_images:
                seed_script = dict(seed_script)
                seed_script["selected_images"] = valid_images
                result_obj["script"] = seed_script
                changed = True

        report = result_obj.get("render_report") if isinstance(result_obj.get("render_report"), dict) else {}
        if audio_path:
            report = dict(report)
            audio_generation = report.get("audio_generation") if isinstance(report.get("audio_generation"), dict) else {}
            audio_generation = dict(audio_generation)
            if str(audio_generation.get("output_path") or "").strip() != audio_path:
                audio_generation["output_path"] = audio_path
                changed = True
            audio_generation["recovery_validated_duration_sec"] = round(float(audio_duration or 0.0), 3)
            report["audio_generation"] = audio_generation
            result_obj["render_report"] = report

        images_ok = bool(valid_images) and _selected_images_ok(valid_images)
        audio_ok = bool(audio_path) and _file_ok(audio_path) and _recovery_audio_duration_plausible(audio_duration, target_minutes)
        render_only = bool(script_ok and images_ok and audio_ok)
        payload["force_render_only"] = render_only

        recovery = result_obj.get("recovery_checkpoint") if isinstance(result_obj.get("recovery_checkpoint"), dict) else {}
        recovery = dict(recovery)
        recovery.update({
            "version": 2,
            "strategy": "highest_valid_checkpoint",
            "script_ok": bool(script_ok),
            "valid_image_count": len(valid_images),
            "audio_ok": bool(audio_ok),
            "audio_duration_sec": round(float(audio_duration or 0.0), 3),
            "target_minutes": int(target_minutes or 0),
            "force_render_only": bool(render_only),
            "paid_stage_regeneration_required": not bool(render_only),
        })
        result_obj["recovery_checkpoint"] = recovery
        changed = True

        if changed:
            try:
                row.result_json = json.dumps(result_obj, ensure_ascii=False)
                db.commit()
            except Exception:
                db.rollback()

        return payload
    finally:
        db.close()

# CODEXIA_RECOVERY_CHECKPOINT_V2_END
'''


def _strip_existing(text: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(START)}.*?{re.escape(END)}\n?",
        flags=re.DOTALL,
    )
    return pattern.sub("\n", text).rstrip() + "\n"


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    text = _strip_existing(text)
    TARGET.write_text(text.rstrip() + BLOCK + "\n", encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        START,
        'strategy": "highest_valid_checkpoint"',
        'payload["force_render_only"] = render_only',
        '"paid_stage_regeneration_required": not bool(render_only)',
        "_recovery_audio_duration_plausible",
        "scene_visuals",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise RuntimeError(f"recovery checkpoint hardening ausente: {missing}")
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
