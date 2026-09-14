from __future__ import annotations

import math
from typing import Any, Callable, Dict, List


BudgetGuard = Callable[..., Dict[str, Any]]


def _rank_for_motion(scene: Dict[str, Any]) -> tuple[int, int]:
    purpose = str(scene.get("purpose") or "story").strip().lower()
    priority = {
        "hook": 0,
        "climax": 1,
        "tension": 2,
        "story": 3,
        "application": 4,
        "prayer": 5,
        "cta": 6,
    }
    return priority.get(purpose, 3), int(scene.get("index") or 0)


def _cost_brl(
    *,
    scenes: List[Dict[str, Any]],
    content_type: str,
    duration_minutes: int,
    fx: float,
) -> float:
    total_motion = sum(max(0, int(s.get("generative_video_seconds") or 0)) for s in scenes)
    premium = sum(
        max(0, int(s.get("generative_video_seconds") or 0))
        for s in scenes
        if str(s.get("tier") or "").upper() == "A"
    )
    economy = max(0, total_motion - premium)
    images_usd = 0.02 * max(1, len(scenes))
    voice_usd = max(1, int(duration_minutes)) * 0.10
    claude_usd = 0.35 if str(content_type or "story").lower() == "story" else 0.22
    motion_usd = premium * 0.11 + economy * 0.05
    subtotal = images_usd + voice_usd + claude_usd + motion_usd
    return subtotal * 1.15 * fx


def _fill_motion_headroom(
    plan: Dict[str, Any],
    *,
    content_type: str,
    duration_minutes: int,
    budget_brl: float,
    fx: float = 5.12,
) -> Dict[str, Any]:
    scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
    clean: List[Dict[str, Any]] = [s for s in scenes if isinstance(s, dict)]
    if not clean:
        return plan

    kind = str(content_type or "story").lower()
    max_motion = max(0, int(plan.get("motion_budget_seconds") or 0))
    if max_motion <= 0:
        ratio = 0.28 if kind == "story" else 0.10 if kind == "devotional" else 0.70
        max_motion = int(round(max(1, int(duration_minutes)) * 60 * ratio))

    # Stories should use most of their cinematic allowance; devotionals should
    # stay more restrained even when the budget is generous.
    utilization = 0.90 if kind == "story" else 0.75 if kind == "devotional" else 0.95
    quality_target = max(0, min(max_motion, int(round(max_motion * utilization))))

    budget_limit = max(1.0, float(budget_brl or 1.0))
    safe_target = budget_limit * 0.95
    fx = max(0.01, float(fx or 5.12))

    current_motion = sum(max(0, int(s.get("generative_video_seconds") or 0)) for s in clean)
    if current_motion >= quality_target:
        guard = plan.get("budget_guard") if isinstance(plan.get("budget_guard"), dict) else {}
        guard["quality_target_motion_seconds"] = quality_target
        guard["motion_fill_added_seconds"] = 0
        guard["budget_utilization_pct"] = round((_cost_brl(scenes=clean, content_type=kind, duration_minutes=duration_minutes, fx=fx) / budget_limit) * 100.0, 1)
        plan["budget_guard"] = guard
        return plan

    current_cost = _cost_brl(scenes=clean, content_type=kind, duration_minutes=duration_minutes, fx=fx)
    headroom_brl = max(0.0, safe_target - current_cost)
    # One economy second costs $0.05 plus the 15% reserve.
    economy_second_brl = 0.05 * 1.15 * fx
    affordable_seconds = max(0, int(math.floor(headroom_brl / economy_second_brl)))
    gap = max(0, quality_target - current_motion)
    seconds_to_add = min(gap, affordable_seconds)
    if seconds_to_add <= 0:
        return plan

    added = 0
    candidates = sorted(clean, key=_rank_for_motion)

    # First extend already-moving B scenes. This is the cheapest way to improve
    # visual continuity because no extra premium provider is introduced.
    for scene in candidates:
        if added >= seconds_to_add:
            break
        tier = str(scene.get("tier") or "C").upper()
        if tier != "B":
            continue
        current = max(0, int(scene.get("generative_video_seconds") or 0))
        target = max(4, min(15, int(scene.get("target_seconds") or 8)))
        desired_cap = min(target, 10)
        room = max(0, desired_cap - current)
        if room <= 0:
            continue
        delta = min(room, seconds_to_add - added)
        scene["generative_video_seconds"] = current + delta
        added += delta

    # Then convert selected still scenes into economical movement. Avoid turning
    # prayer/CTA sections into expensive motion unless absolutely necessary.
    for scene in candidates:
        if added >= seconds_to_add:
            break
        tier = str(scene.get("tier") or "C").upper()
        purpose = str(scene.get("purpose") or "story").lower()
        if tier != "C" or purpose in {"prayer", "cta"}:
            continue
        target = max(4, min(15, int(scene.get("target_seconds") or 8)))
        delta = min(min(target, 8), seconds_to_add - added)
        if delta <= 0:
            continue
        scene["tier"] = "B"
        scene["recommended_provider"] = "runway"
        scene["generative_video_seconds"] = delta
        added += delta

    # If the target is still not met, use calm application scenes before prayer/CTA.
    for scene in candidates:
        if added >= seconds_to_add:
            break
        tier = str(scene.get("tier") or "C").upper()
        purpose = str(scene.get("purpose") or "story").lower()
        if tier != "C" or purpose in {"prayer", "cta"}:
            continue
        current = max(0, int(scene.get("generative_video_seconds") or 0))
        target = max(4, min(15, int(scene.get("target_seconds") or 6)))
        delta = min(max(0, min(target, 6) - current), seconds_to_add - added)
        if delta <= 0:
            continue
        scene["tier"] = "B"
        scene["recommended_provider"] = "runway"
        scene["generative_video_seconds"] = current + delta
        added += delta

    total_motion = sum(max(0, int(s.get("generative_video_seconds") or 0)) for s in clean)
    premium_motion = sum(
        max(0, int(s.get("generative_video_seconds") or 0))
        for s in clean
        if str(s.get("tier") or "").upper() == "A"
    )
    estimated_brl = _cost_brl(scenes=clean, content_type=kind, duration_minutes=duration_minutes, fx=fx)

    # Defensive rollback: if provider-price rounding somehow crosses the safe
    # target, remove only the extra economy seconds we just introduced.
    if estimated_brl > safe_target + 0.01:
        over_brl = estimated_brl - safe_target
        remove_seconds = min(added, int(math.ceil(over_brl / economy_second_brl)))
        for scene in reversed(candidates):
            if remove_seconds <= 0:
                break
            if str(scene.get("tier") or "").upper() != "B":
                continue
            current = max(0, int(scene.get("generative_video_seconds") or 0))
            if current <= 0:
                continue
            cut = min(current, remove_seconds)
            scene["generative_video_seconds"] = current - cut
            remove_seconds -= cut
            added -= cut
            if scene["generative_video_seconds"] <= 0:
                scene["generative_video_seconds"] = 0
                scene["tier"] = "C"
                scene["recommended_provider"] = "still"
        total_motion = sum(max(0, int(s.get("generative_video_seconds") or 0)) for s in clean)
        premium_motion = sum(
            max(0, int(s.get("generative_video_seconds") or 0))
            for s in clean
            if str(s.get("tier") or "").upper() == "A"
        )
        estimated_brl = _cost_brl(scenes=clean, content_type=kind, duration_minutes=duration_minutes, fx=fx)

    plan["scenes"] = clean
    plan["motion_planned_seconds"] = total_motion
    guard = plan.get("budget_guard") if isinstance(plan.get("budget_guard"), dict) else {}
    guard.update(
        {
            "quality_target_motion_seconds": int(quality_target),
            "motion_fill_added_seconds": int(max(0, added)),
            "premium_motion_seconds": int(premium_motion),
            "economy_motion_seconds": int(max(0, total_motion - premium_motion)),
            "total_motion_seconds": int(total_motion),
            "estimated_brl": round(estimated_brl, 2),
            "headroom_brl": round(max(0.0, budget_limit - estimated_brl), 2),
            "budget_utilization_pct": round((estimated_brl / budget_limit) * 100.0, 1),
            "budget_respected": estimated_brl <= budget_limit,
        }
    )
    plan["budget_guard"] = guard
    checks = plan.get("quality_checks") if isinstance(plan.get("quality_checks"), dict) else {}
    checks["budget_respected"] = estimated_brl <= budget_limit
    plan["quality_checks"] = checks
    return plan


def wrap_budget_guard(base_guard: BudgetGuard, *, fx_getter: Callable[[], float]) -> BudgetGuard:
    """Wrap the ceiling guard with a quality pass that uses safe unused headroom."""

    def guard(
        plan: Dict[str, Any],
        *,
        content_type: str,
        duration_minutes: int,
        budget_brl: float,
    ) -> Dict[str, Any]:
        balanced = base_guard(
            plan,
            content_type=content_type,
            duration_minutes=duration_minutes,
            budget_brl=budget_brl,
        )
        return _fill_motion_headroom(
            balanced,
            content_type=content_type,
            duration_minutes=duration_minutes,
            budget_brl=budget_brl,
            fx=fx_getter(),
        )

    return guard
