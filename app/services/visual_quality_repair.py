from __future__ import annotations

from copy import deepcopy
from math import ceil
from typing import Any, Dict, Iterable, List


_IMAGE_PATH_KEYS = ("image_path", "image_url", "path", "url", "storage_key")


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _positive_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _paths(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    out: List[str] = []
    seen = set()
    for item in items:
        value = ""
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            for key in _IMAGE_PATH_KEYS:
                value = str(item.get(key) or "").strip()
                if value:
                    break
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def rendered_visual_diversity_report(
    render_report: Any,
    *,
    available_image_count: int = 0,
    recovered_render: bool = False,
) -> Dict[str, Any]:
    """Judge the images actually assigned to rendered scenes, not cache reuse."""
    report = render_report if isinstance(render_report, dict) else {}
    visual_plan = report.get("visual_plan") if isinstance(report.get("visual_plan"), dict) else {}
    scene_visuals = report.get("scene_visuals") if isinstance(report.get("scene_visuals"), list) else []
    rendered_paths = _paths(scene_visuals)

    planned_count = max(
        _positive_int(visual_plan.get("requested_image_count")),
        _positive_int(visual_plan.get("group_count")),
    )
    scene_count = len(scene_visuals)
    expected_count = max(planned_count, len(rendered_paths), _positive_int(available_image_count))
    if expected_count <= 0:
        expected_count = scene_count
    # A quality render should use several distinct visuals. For larger plans,
    # allow a small amount of deliberate reuse while rejecting one-image cuts.
    scene_limit = scene_count or expected_count
    minimum_unique = (
        1
        if expected_count <= 1
        else min(scene_limit, expected_count, max(2, int(ceil(expected_count * 0.75))))
    )

    beat_holds: List[float] = []
    previous_path = ""
    for item in scene_visuals:
        if not isinstance(item, dict):
            continue
        paths = _paths([item])
        path = paths[0] if paths else ""
        hold = _positive_float(
            item.get("final_visual_duration_sec")
            or item.get("visual_duration_sec")
            or item.get("max_visual_hold_sec")
        )
        if path and hold:
            if path == previous_path and beat_holds:
                beat_holds[-1] += hold
            else:
                beat_holds.append(hold)
        previous_path = path
    resource_profile = report.get("resource_profile") if isinstance(report.get("resource_profile"), dict) else {}
    hold_target = _positive_float(resource_profile.get("visual_hold_target_sec"))
    # The director's preferred pace is advisory. A user-approved duration
    # must not create a tighter, invented rejection threshold.
    hold_limit = 30.0
    visual_plan_avg = _positive_float(visual_plan.get("average_image_duration_sec"))

    if rendered_paths:
        count_ok = len(rendered_paths) >= minimum_unique
        if beat_holds:
            pacing_ok = max(beat_holds) <= hold_limit
        else:
            pacing_ok = visual_plan_avg <= 30.0 if visual_plan_avg > 0 else True
        passed = bool(count_ok and pacing_ok)
        evidence = "rendered_scene_image_paths"
    elif recovered_render and _positive_int(available_image_count) >= 2:
        # A recovered MP4 can lose its scene report. It remains reviewable when
        # frame extraction found multiple images; a human can assess variety.
        count_ok = True
        pacing_ok = True
        passed = True
        evidence = "recovered_render_frames_for_human_review"
    else:
        count_ok = False
        pacing_ok = False
        passed = False
        evidence = "rendered_scene_evidence_missing"

    return {
        "passed": passed,
        "evidence": evidence,
        "planned_image_count": planned_count,
        "rendered_scene_count": scene_count,
        "unique_rendered_image_count": len(rendered_paths),
        "minimum_unique_image_count": minimum_unique,
        "count_ok": count_ok,
        "pacing_ok": pacing_ok,
        "visual_hold_target_sec": round(hold_target, 3),
        "visual_hold_limit_sec": round(hold_limit, 3),
        "max_visual_beat_hold_sec": round(max(beat_holds), 3) if beat_holds else None,
        "legacy_average_image_duration_sec": round(visual_plan_avg, 3) if visual_plan_avg else None,
        "path_reuse_count_is_advisory": _positive_int(visual_plan.get("reused_image_count")),
        "review_recommended": bool(
            evidence == "recovered_render_frames_for_human_review"
            or (hold_target and beat_holds and max(beat_holds) > hold_target)
        ),
    }


def visual_failure_message(check: str, details: Dict[str, Any], attempts: int = 0) -> str:
    visual = (details.get("director_quality") or {}).get("visual_variety") or {}
    images = details.get("images") or {}
    if check == "image_count_minimum":
        reason = f"Arquivos de imagens disponíveis: {images.get('actual_found', 0)}; mínimo necessário: {images.get('expected_min', 0)}."
    elif check == "image_files_exist":
        reason = "Nenhum arquivo de imagem disponível foi encontrado."
    elif not visual.get("count_ok", True):
        reason = f"Imagens distintas no vídeo: {visual.get('unique_rendered_image_count', 0)}; mínimo necessário: {visual.get('minimum_unique_image_count', 0)}."
    elif not visual.get("pacing_ok", True):
        reason = f"Uma imagem permanece continuamente por {visual.get('max_visual_beat_hold_sec') or visual.get('legacy_average_image_duration_sec', 0)}s; limite: {visual.get('visual_hold_limit_sec', 30)}s."
    else:
        reason = "Não foi possível comprovar a variedade visual pelo relatório do render."
    prefix = f"Após {attempts} tentativa(s) de correção automática, " if attempts else ""
    return prefix + reason + " O vídeo e os ativos foram preservados."


def build_auto_visual_repair_plan(
    script: Any,
    render_report: Any,
    *,
    expected_image_count: int = 0,
    task_id: str = "",
    attempt: int = 1,
    max_new_image_calls: int = 8,
    audio_path: str = "",
    narration_text: str = "",
    reuse_audio: bool = True,
    replace_repeated_visuals: bool = False,
) -> Dict[str, Any]:
    """Build a bounded, same-task repair plan using every valid image first.

    If the rendered video failed the diversity check, use only distinct images
    that actually appeared in the MP4. Unused cached images do not help repair
    a render that repeated one image across scenes.
    """
    plan = deepcopy(script) if isinstance(script, dict) else {}
    report = render_report if isinstance(render_report, dict) else {}
    visual_plan = report.get("visual_plan") if isinstance(report.get("visual_plan"), dict) else {}
    scene_visuals = report.get("scene_visuals") if isinstance(report.get("scene_visuals"), list) else []

    images: List[str] = []
    for key in ("selected_images", "custom_image_paths", "images"):
        images.extend(_paths(plan.get(key)))
    images.extend(_paths(scene_visuals))
    images = list(dict.fromkeys(images))
    if replace_repeated_visuals:
        # Trust what the renderer actually used, not the larger unused cache.
        # Repeated scene assignments collapse to unique paths here, so the
        # partial-recovery budget fills the remaining visual groups with fresh
        # images while preserving each distinct rendered image once.
        images = _paths(scene_visuals)

    scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
    if len(scenes) < 2:
        return {"ok": False, "reason": "fewer_than_two_story_scenes", "plan": plan}

    planned_count = max(
        _positive_int(expected_image_count),
        _positive_int(plan.get("strict_visual_target_count")),
        _positive_int(plan.get("expected_image_count")),
        _positive_int(visual_plan.get("requested_image_count")),
        _positive_int(visual_plan.get("group_count")),
        len(images),
        2,
    )
    target_count = min(64, len(scenes), planned_count)
    if target_count < 2:
        return {"ok": False, "reason": "visual_target_below_two", "plan": plan}

    available_count = min(target_count, len(images))
    missing_count = max(0, target_count - available_count)
    max_new = min(missing_count, max(0, _positive_int(max_new_image_calls)))
    plan["selected_images"] = images[:target_count]
    plan["image_mode"] = "multiple"
    plan["single_bg"] = False
    plan["allow_image_reuse"] = False
    plan["force_reuse_assets"] = bool(images)
    plan["force_render_only"] = False
    plan["repair_mode"] = True
    plan["repair_complete_visuals"] = True
    plan["strict_visual_quality_required"] = True
    plan["expected_image_count"] = target_count
    plan["strict_visual_target_count"] = target_count
    plan["automatic_visual_repair"] = {
        "task_id": str(task_id or ""),
        "attempt": max(1, _positive_int(attempt)),
        "target_image_count": target_count,
        "preserved_image_count": available_count,
        "max_new_image_calls": max_new,
        "replacing_repeated_visuals": bool(replace_repeated_visuals),
    }

    if missing_count > 0:
        budget = {
            "enabled": True,
            "existing_image_count": available_count,
            "expected_image_count": target_count,
            "missing_image_count": missing_count,
            "max_new_image_calls": max_new,
            "plan_hash": f"auto-visual-repair:{task_id}:{attempt}:{available_count}:{target_count}",
        }
        plan["_partial_image_recovery"] = dict(budget)
        plan["repair_image_budget"] = dict(budget)
    else:
        plan.pop("_partial_image_recovery", None)
        plan.pop("repair_image_budget", None)

    audio = report.get("audio_generation") if isinstance(report.get("audio_generation"), dict) else {}
    resolved_audio_path = str(
        audio_path
        or audio.get("output_path")
        or audio.get("final_audio_path")
        or audio.get("audio_path")
        or plan.get("seed_audio_path")
        or ""
    ).strip()
    resolved_narration = str(
        narration_text
        or (report.get("narration_plan") or {}).get("full_text")
        or audio.get("final_text_sent_to_tts")
        or plan.get("seed_narration_text")
        or ""
    ).strip()
    approved_audio = bool(plan.get("approved_narration_required") or plan.get("tts_locked"))
    if reuse_audio or approved_audio:
        if resolved_audio_path:
            plan["seed_audio_path"] = resolved_audio_path
        if resolved_narration:
            plan["seed_narration_text"] = resolved_narration
    else:
        plan.pop("seed_audio_path", None)
        plan.pop("reuse_audio_from", None)
        if approved_audio:
            return {"ok": False, "reason": "approved_audio_cannot_be_regenerated", "plan": plan}

    return {
        "ok": True,
        "plan": plan,
        "target_image_count": target_count,
        "preserved_image_count": available_count,
        "missing_image_count": missing_count,
        "max_new_image_calls": max_new,
        "reuse_audio": bool(reuse_audio or approved_audio),
    }
