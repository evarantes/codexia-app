from __future__ import annotations

import hashlib
import json
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


def build_sparse_visual_optimization_plan(
    *,
    task_id: str,
    title: str,
    target_visual_count: int,
    valid_image_paths: Iterable[Any],
    script: Dict[str, Any],
    audio_path: str,
    image_unit_cost_usd: float = 0.0,
) -> Dict[str, Any]:
    """Build a zero-paid-media proposal for sparse visual recovery.

    The plan never alters script/narration content. It only changes how already
    valid visuals are distributed over the existing narration timeline.
    Execution must require explicit confirmation of ``plan_hash`` whenever the
    optimization is actually needed.
    """
    images = _clean_paths(valid_image_paths)
    target = max(0, int(target_visual_count or 0))
    existing = len(images)
    shortage = max(0, target - existing)
    script_obj = dict(script or {}) if isinstance(script, dict) else {}
    audio = str(audio_path or "").strip()
    unit = max(0.0, float(image_unit_cost_usd or 0.0))

    eligible = bool(script_obj and audio and images and target > 0 and shortage > 0)
    canonical = {
        "version": 1,
        "task_id": str(task_id or "").strip(),
        "strategy": "ordered_adjacent_visual_reuse_v1",
        "target_visual_count": target,
        "valid_image_paths": images,
        "script_sha256": _stable_hash(script_obj),
        "audio_path": audio,
        "preserve_full_script": True,
        "preserve_full_narration": True,
        "paid_image_calls": 0,
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
        "quality_policy": {
            "reuse_only_adjacent_narrative_groups": True,
            "keep_original_visual_order": True,
            "extend_visual_hold_when_needed": True,
            "never_shorten_narration": True,
            "never_remove_script_text": True,
            "never_generate_paid_images": True,
        },
        "plan_hash": plan_hash,
    }


def validate_optimization_confirmation(plan: Dict[str, Any], supplied_hash: Any) -> bool:
    if not isinstance(plan, dict) or not bool(plan.get("requires_confirmation")):
        return True
    expected = str(plan.get("plan_hash") or "").strip()
    supplied = str(supplied_hash or "").strip()
    return bool(expected and supplied and hashlib.compare_digest(expected, supplied))
