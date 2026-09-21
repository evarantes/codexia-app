from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from typing import Any, Dict, Iterable, List


def _clean_paths(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in out:
            out.append(item)
    return out


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def minimum_visual_count_for_duration(
    duration_minutes: Any,
    *,
    max_average_image_seconds: float = 30.0,
    scenes_per_minute: float = 2.0,
) -> int:
    """Return the minimum visual density required by the V2 quality contract.

    Two independent limits are enforced: no visual should need to carry more
    than the configured average hold time, and the current cinematic target is
    roughly two scenes per minute. The stricter result wins.
    """
    try:
        minutes = max(0.0, float(duration_minutes or 0.0))
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes <= 0.0:
        return 0

    try:
        hold_seconds = max(1.0, float(max_average_image_seconds or 30.0))
    except (TypeError, ValueError):
        hold_seconds = 30.0
    try:
        density = max(0.1, float(scenes_per_minute or 2.0))
    except (TypeError, ValueError):
        density = 2.0

    by_hold = int(math.ceil((minutes * 60.0) / hold_seconds))
    by_density = int(math.ceil(minutes * density))
    return max(1, by_hold, by_density)


def build_visual_quality_completion_plan(
    *,
    task_id: str,
    title: str,
    duration_minutes: Any,
    requested_target_visual_count: int,
    valid_image_paths: Iterable[Any],
    script: Dict[str, Any],
    audio_path: str,
    image_unit_cost_usd: float = 0.0,
    audio_unit_cost_usd_per_minute: float | None = None,
    usd_brl: float | None = None,
    regenerate_narration: bool = True,
) -> Dict[str, Any]:
    """Plan a strict V2 correction with explicit cost confirmation.

    The plan reuses every valid paid image before authorizing new image calls,
    binds the displayed cost assumptions into the confirmation hash, and keeps
    the paid-image call count as a hard execution ceiling.
    """
    images = _clean_paths(valid_image_paths)
    try:
        requested = max(0, int(requested_target_visual_count or 0))
    except (TypeError, ValueError):
        requested = 0
    quality_floor = minimum_visual_count_for_duration(duration_minutes)
    target = max(requested, quality_floor)
    existing = len(images)
    missing = max(0, target - existing)
    script_obj = dict(script or {}) if isinstance(script, dict) else {}
    audio = str(audio_path or "").strip()

    try:
        image_unit = max(0.0, float(image_unit_cost_usd or 0.0))
    except (TypeError, ValueError):
        image_unit = 0.0

    if audio_unit_cost_usd_per_minute is None:
        try:
            audio_unit = max(
                0.0,
                float(os.getenv("YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT") or "0.0120"),
            )
        except Exception:
            audio_unit = 0.0120
    else:
        try:
            audio_unit = max(0.0, float(audio_unit_cost_usd_per_minute or 0.0))
        except (TypeError, ValueError):
            audio_unit = 0.0

    if usd_brl is None:
        try:
            fx = max(0.01, float(os.getenv("CODEXIA_USD_BRL") or "5.20"))
        except Exception:
            fx = 5.20
    else:
        try:
            fx = max(0.01, float(usd_brl or 0.0))
        except (TypeError, ValueError):
            fx = 5.20

    try:
        minutes = max(0.0, float(duration_minutes or 0.0))
    except (TypeError, ValueError):
        minutes = 0.0

    # Em falhas exclusivamente visuais (ex.: image_count_minimum), uma narração
    # já validada não deve ser cobrada nem regenerada. Se o áudio não existir,
    # a reconstrução continua obrigatória mesmo quando o chamador pediu reuso.
    should_regenerate_narration = bool(regenerate_narration or not audio)

    image_cost_usd = round(image_unit * missing, 6)
    image_cost_brl = round(image_cost_usd * fx, 2)
    audio_cost_usd = round(audio_unit * minutes, 6) if should_regenerate_narration else 0.0
    audio_cost_brl = round(audio_cost_usd * fx, 2)
    total_cost_usd = round(image_cost_usd + audio_cost_usd, 6)
    total_cost_brl = round(total_cost_usd * fx, 2)

    canonical = {
        "version": 5,
        "task_id": str(task_id or "").strip(),
        "strategy": "quality_completion_preserve_then_generate_v2",
        "strict_visual_quality_required": True,
        "duration_minutes": minutes,
        "target_visual_count": target,
        "quality_floor_visual_count": quality_floor,
        "valid_image_paths": images,
        "script_sha256": _stable_hash(script_obj),
        "audio_path": audio,
        "preserve_full_script": True,
        "preserve_existing_images": True,
        "preserve_full_narration": bool(audio) and not should_regenerate_narration,
        "regenerate_narration": should_regenerate_narration,
        "paid_image_calls": missing,
        "max_new_image_calls": missing,
        "image_unit_cost_usd": round(image_unit, 6),
        "audio_unit_cost_usd_per_minute": round(audio_unit, 6),
        "usd_brl": round(fx, 6),
        "estimated_new_image_cost_usd": image_cost_usd,
        "estimated_new_image_cost_brl": image_cost_brl,
        "estimated_new_audio_cost_usd": audio_cost_usd,
        "estimated_new_audio_cost_brl": audio_cost_brl,
        "estimated_total_additional_cost_usd": total_cost_usd,
        "estimated_total_additional_cost_brl": total_cost_brl,
        "auto_publish": False,
    }
    plan_hash = _stable_hash(canonical)

    return {
        **canonical,
        "title": str(title or "").strip(),
        "valid_image_count": existing,
        "missing_visual_count": missing,
        "quality_completion_required": True,
        "optimization_required": False,
        "requires_confirmation": True,
        "estimated_image_calls_avoided": existing,
        "estimated_new_image_calls": missing,
        "estimated_savings_usd": round(image_unit * existing, 6) if image_unit > 0 else None,
        "estimated_savings_brl": round(image_unit * existing * fx, 2) if image_unit > 0 else None,
        "audio_provider_policy": (
            "premium_configured_then_free_fallback"
            if should_regenerate_narration
            else "reuse_valid_existing_audio"
        ),
        "cost_confirmation": {
            "currency_reference": "USD",
            "usd_brl": round(fx, 6),
            "image_unit_cost_usd": round(image_unit, 6),
            "audio_unit_cost_usd_per_minute": round(audio_unit, 6),
            "max_new_image_calls": missing,
            "estimated_image_cost_usd": image_cost_usd,
            "estimated_image_cost_brl": image_cost_brl,
            "estimated_audio_cost_usd": audio_cost_usd,
            "estimated_audio_cost_brl": audio_cost_brl,
            "estimated_total_cost_usd": total_cost_usd,
            "estimated_total_cost_brl": total_cost_brl,
            "paid_image_call_cap_is_hard": True,
            "monetary_values_are_estimates": True,
        },
        "quality_policy": {
            "reuse_existing_images_once_before_generation": True,
            "generate_only_missing_visuals": True,
            "max_average_image_seconds": 30.0,
            "minimum_scenes_per_minute": 2.0,
            "never_shorten_narration": True,
            "preserve_existing_narration_when_valid": not should_regenerate_narration,
            "never_remove_script_text": True,
            "require_audio_timed_captions": True,
            "hard_cap_new_image_calls": missing,
        },
        "plan_hash": plan_hash,
    }


def proportional_visual_index(group_index: int, image_count: int, group_count: int) -> int:
    """Map ordered narrative groups to ordered images without round-robin jumps."""
    images = max(1, int(image_count or 1))
    groups = max(1, int(group_count or 1))
    group = max(0, min(int(group_index or 0), groups - 1))
    if images >= groups:
        return min(group, images - 1)
    return min(images - 1, (group * images) // groups)


def build_sparse_visual_optimization_plan(
    *,
    task_id: str,
    title: str,
    target_visual_count: int,
    valid_image_paths: Iterable[Any],
    script: Dict[str, Any],
    audio_path: str,
    image_unit_cost_usd: float = 0.0,
    lightweight_recovery: bool = False,
) -> Dict[str, Any]:
    """Build an explain-before-act recovery proposal using only local paid assets.

    The proposal never alters the script or narration. It may reuse already-paid
    images in adjacent narrative groups and, when ``lightweight_recovery`` is
    requested, switch only the final renderer from per-frame MoviePy motion to a
    local FFmpeg still-image/caption pipeline. Any strategy change is bound into
    ``plan_hash`` and therefore requires exact user confirmation before execution.
    """
    images = _clean_paths(valid_image_paths)
    target = max(0, int(target_visual_count or 0))
    existing = len(images)
    shortage = max(0, target - existing)
    script_obj = dict(script or {}) if isinstance(script, dict) else {}
    audio = str(audio_path or "").strip()
    unit = max(0.0, float(image_unit_cost_usd or 0.0))
    lightweight = bool(lightweight_recovery)

    eligible = bool(
        script_obj
        and audio
        and images
        and target > 0
        and (shortage > 0 or lightweight)
    )
    canonical = {
        "version": 2,
        "task_id": str(task_id or "").strip(),
        "strategy": "ordered_adjacent_visual_reuse_v1",
        "render_strategy": "ffmpeg_lightweight_recovery_v1" if lightweight else "original_renderer",
        "lightweight_recovery": lightweight,
        "target_visual_count": target,
        "valid_image_paths": images,
        "script_sha256": _stable_hash(script_obj),
        "audio_path": audio,
        "preserve_full_script": True,
        "preserve_full_narration": True,
        "preserve_captions": True,
        "paid_image_calls": 0,
        "paid_tts_calls": 0,
        "external_music_provider_calls": 0 if lightweight else None,
    }
    plan_hash = _stable_hash(canonical)

    return {
        **canonical,
        "title": str(title or "").strip(),
        "valid_image_count": existing,
        "missing_visual_count": shortage,
        "optimization_required": bool(eligible),
        "requires_confirmation": bool(eligible),
        "estimated_image_calls_avoided": shortage if eligible else 0,
        "estimated_savings_usd": round(unit * shortage, 6) if eligible and unit > 0 else None,
        "render_policy": {
            "renderer": "ffmpeg_concat_subtitles_v1" if lightweight else "original_renderer",
            "simplify_heavy_camera_motion": lightweight,
            "keep_caption_timing": True,
            "keep_branded_endcard_when_locally_renderable": lightweight,
            "use_only_existing_local_music": lightweight,
            "never_call_external_music_provider": lightweight,
            "never_download_music_during_recovery": lightweight,
        },
        "quality_policy": {
            "reuse_only_adjacent_narrative_groups": True,
            "keep_original_visual_order": True,
            "extend_visual_hold_when_needed": True,
            "never_shorten_narration": True,
            "never_remove_script_text": True,
            "never_remove_captions": True,
            "never_generate_paid_images": True,
            "never_regenerate_paid_tts": True,
        },
        "plan_hash": plan_hash,
    }


def validate_optimization_confirmation(plan: Dict[str, Any], supplied_hash: Any) -> bool:
    if not isinstance(plan, dict) or not bool(plan.get("requires_confirmation")):
        return True
    expected = str(plan.get("plan_hash") or "").strip()
    supplied = str(supplied_hash or "").strip()
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))
