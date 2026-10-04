"""Helpers for correcting narration length from measured TTS audio."""
from __future__ import annotations

import math


def _non_negative(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def planning_duration_bounds(
    min_total_sec: float,
    max_total_sec: float,
    target_total_sec: float,
) -> tuple[float, float]:
    """Return a feasible planning band around a requested narration duration.

    The old 98% lower bound and 96% upper bound crossed for exact-duration
    requests. Keep the 2% lower tolerance, and give the upper bound 2% room.
    If callers supply inconsistent bounds, widen the upper edge to the lower.
    """
    minimum = _non_negative(min_total_sec)
    maximum = _non_negative(max_total_sec)
    target = _non_negative(target_total_sec)

    lower = max(minimum * 0.97, target * 0.98)
    upper = maximum * 1.02 if maximum else 0.0
    if upper and lower > upper:
        upper = lower
    return lower, upper


def calibrated_body_duration_target(
    estimated_body_sec: float,
    actual_audio_sec: float,
    fixed_audio_sec: float,
    desired_audio_sec: float,
) -> float:
    """Translate a measured audio target into the text estimator's time scale.

    TTS pace often differs from the static word-per-minute estimate. Calibrate
    the next body target by the measured body pace while keeping the opening,
    CTA, silence, and pause outside the body calculation.
    """
    estimate = _non_negative(estimated_body_sec)
    actual = _non_negative(actual_audio_sec)
    fixed = _non_negative(fixed_audio_sec)
    desired = _non_negative(desired_audio_sec)
    actual_body = max(0.0, actual - fixed)
    desired_body = max(0.0, desired - fixed)
    if desired_body <= 0:
        return 0.0
    if estimate <= 0 or actual_body <= 0:
        return desired_body
    return estimate * desired_body / actual_body
