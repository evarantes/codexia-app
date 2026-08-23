from __future__ import annotations

import math
import os
from typing import Any, Dict, Iterable, List, Optional


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return int(default)


def _existing_file(path: Any, minimum_bytes: int = 1000) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        if os.path.isfile(raw) and os.path.getsize(raw) >= minimum_bytes:
            return os.path.abspath(raw)
    except Exception:
        return ""
    return ""


def _artifact_image_paths(manifest: Dict[str, Any]) -> List[str]:
    paths: List[str] = []
    for item in manifest.get("artifacts") or []:
        if not isinstance(item, dict) or str(item.get("kind") or "").lower() != "image":
            continue
        candidate = _existing_file(item.get("durable_path") or item.get("original_path"))
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def ordered_preserved_images(manifest: Any) -> List[str]:
    """Return valid durable images, preferring the original narrative order."""
    data = _as_dict(manifest)
    artifacts = [item for item in data.get("artifacts") or [] if isinstance(item, dict)]
    by_original: Dict[str, str] = {}
    by_basename: Dict[str, str] = {}
    for item in artifacts:
        if str(item.get("kind") or "").lower() != "image":
            continue
        durable = _existing_file(item.get("durable_path") or item.get("original_path"))
        if not durable:
            continue
        original = str(item.get("original_path") or "").strip()
        if original:
            by_original[original] = durable
            by_basename.setdefault(os.path.basename(original), durable)
        by_basename.setdefault(os.path.basename(durable), durable)

    ordered: List[str] = []
    for ref in data.get("selected_image_references") or []:
        raw = str(ref or "").strip()
        if not raw:
            continue
        direct = _existing_file(raw)
        candidate = direct or by_original.get(raw) or by_basename.get(os.path.basename(raw), "")
        if candidate and candidate not in ordered:
            ordered.append(candidate)

    for candidate in _artifact_image_paths(data):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def extract_script(task_result: Any, payload: Any, manifest: Any) -> Dict[str, Any]:
    result = _as_dict(task_result)
    payload_dict = _as_dict(payload)
    manifest_dict = _as_dict(manifest)
    for source in (manifest_dict, result, payload_dict):
        for key in ("script", "seeded_script", "storyboard", "editorial_script"):
            candidate = source.get(key) if isinstance(source, dict) else None
            if isinstance(candidate, dict) and candidate:
                return dict(candidate)
    return {}


def resolve_scene_count(script: Any, manifest: Any, task_result: Any = None) -> int:
    script_dict = _as_dict(script)
    manifest_dict = _as_dict(manifest)
    result = _as_dict(task_result)
    candidates: List[int] = []
    scenes = script_dict.get("scenes")
    if isinstance(scenes, list) and scenes:
        candidates.append(len(scenes))
    for source in (manifest_dict, result, script_dict):
        for key in ("expected_image_count", "scene_count"):
            value = _as_int(source.get(key))
            if value > 0:
                candidates.append(value)
    render = _as_dict(result.get("render_report"))
    scene_visuals = render.get("scene_visuals")
    if isinstance(scene_visuals, list) and scene_visuals:
        candidates.append(len(scene_visuals))
    return max(candidates or [0])


def resolve_duration_seconds(task_result: Any, payload: Any, manifest: Any) -> float:
    result = _as_dict(task_result)
    payload_dict = _as_dict(payload)
    manifest_dict = _as_dict(manifest)
    render = _as_dict(result.get("render_report"))
    duration_plan = _as_dict(render.get("duration_plan"))
    for value in (
        duration_plan.get("obtained_duration_sec"),
        duration_plan.get("target_video_duration_sec"),
        duration_plan.get("actual_audio_duration_sec"),
        render.get("narration_duration_sec"),
        result.get("narration_duration_sec"),
    ):
        seconds = _as_float(value)
        if seconds > 0:
            return seconds
    for source in (payload_dict, manifest_dict):
        minutes = _as_float(source.get("duration") or source.get("duration_minutes") or source.get("expected_duration_minutes"))
        if minutes > 0:
            return minutes * 60.0
    return 0.0


def required_unique_visuals(duration_sec: float, scene_count: int, seconds_per_image: float = 15.0) -> int:
    duration = max(0.0, _as_float(duration_sec))
    scenes = max(0, _as_int(scene_count))
    target_hold = max(10.0, min(30.0, _as_float(seconds_per_image, 15.0)))
    if duration <= 0:
        return scenes if scenes > 0 else 0
    required = max(1, int(math.ceil(duration / target_hold)))
    if scenes > 0:
        required = min(required, scenes)
    return required


def build_repair_preview(
    *,
    task_id: str,
    title: str,
    task_result: Any,
    payload: Any,
    manifest: Any,
    image_cost_unit: float = 0.0,
    seconds_per_image: float = 15.0,
) -> Dict[str, Any]:
    payload_dict = _as_dict(payload)
    script = extract_script(task_result, payload_dict, manifest)
    scene_count = resolve_scene_count(script, manifest, task_result)
    duration_sec = resolve_duration_seconds(task_result, payload_dict, manifest)
    required = required_unique_visuals(duration_sec, scene_count, seconds_per_image)
    preserved = ordered_preserved_images(manifest)
    if required > 0:
        preserved = preserved[:required]
    existing = len(preserved)
    missing = max(0, required - existing)
    unit_cost = max(0.0, _as_float(image_cost_unit))
    return {
        "task_id": str(task_id or ""),
        "title": str(title or "").strip(),
        "duration_sec": round(duration_sec, 2),
        "duration_minutes": round(duration_sec / 60.0, 2) if duration_sec > 0 else 0.0,
        "scene_count": int(scene_count),
        "required_unique_image_count": int(required),
        "existing_image_count": int(existing),
        "missing_image_count": int(missing),
        "preserved_images": preserved,
        "regenerate_audio": True,
        "reuse_old_audio": False,
        "reuse_old_mp4": False,
        "preserve_script": bool(script),
        "image_cost_unit": round(unit_cost, 6),
        "estimated_new_image_cost": round(unit_cost * missing, 6) if unit_cost > 0 else None,
        "paid_image_calls_require_confirmation": bool(missing > 0),
        "publication_blocked_during_repair": True,
        "seconds_per_unique_image_target": float(seconds_per_image),
    }


def build_confirmed_image_budget(preview: Any, *, max_new_images: int) -> Dict[str, Any]:
    data = _as_dict(preview)
    missing = max(0, _as_int(data.get("missing_image_count")))
    confirmed = max(0, _as_int(max_new_images))
    if confirmed != missing:
        raise ValueError(
            f"A confirmação precisa corresponder ao plano atual: {missing} novas imagens, recebido {confirmed}."
        )
    unit = max(0.0, _as_float(data.get("image_cost_unit")))
    expected = max(0, _as_int(data.get("required_unique_image_count")))
    existing = max(0, _as_int(data.get("existing_image_count")))
    return {
        "enabled": True,
        "expected_image_count": expected,
        "existing_image_count": min(existing, expected) if expected else existing,
        "missing_image_count": missing,
        "estimated_image_cost_usd": round(unit * missing, 6) if unit > 0 else 0.0,
        "estimated_image_cost_brl": 0.0,
        "plan_hash": f"repair:{data.get('task_id') or ''}:{expected}:{existing}:{missing}",
    }
