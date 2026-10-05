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


def playback_rate_for_target(
    speech_duration_sec: float,
    fixed_duration_sec: float,
    target_total_sec: float,
    *,
    minimum_rate: float = 0.90,
) -> float | None:
    """Return a pitch-preserving slowdown rate for a small duration deficit.

    Fixed visual opening silence and pauses are excluded. ``None`` means the
    shortfall is too large to correct without materially slowing the voice, so
    the caller should replan the narration text instead.
    """
    speech = _non_negative(speech_duration_sec)
    fixed = _non_negative(fixed_duration_sec)
    target = _non_negative(target_total_sec)
    try:
        min_rate = float(minimum_rate)
    except (TypeError, ValueError):
        min_rate = 0.90
    if not math.isfinite(min_rate):
        min_rate = 0.90
    min_rate = max(0.5, min(1.0, min_rate))
    target_speech = target - fixed
    if speech <= 0 or target_speech <= speech:
        return None
    rate = speech / target_speech
    if rate < min_rate or rate >= 1.0:
        return None
    return round(rate, 6)


def expansion_word_range(
    current_words: int,
    estimated_body_sec: float,
    target_body_sec: float,
    words_per_minute: float,
) -> tuple[int, int, int]:
    """Choose a proportional word range for a short narration correction."""
    try:
        words = max(0, int(current_words))
    except (TypeError, ValueError):
        words = 0
    estimate = _non_negative(estimated_body_sec)
    target = _non_negative(target_body_sec)
    wpm = _non_negative(words_per_minute) or 150.0
    if words <= 0 or target <= 0:
        return 0, 0, 0

    duration_ratio = target / estimate if estimate > 0 else 1.0
    proportional_words = math.ceil(words * duration_ratio * 1.04)
    wpm_words = math.ceil(target * wpm / 60.0)
    target_words = max(words + max(8, math.ceil(words * 0.04)), proportional_words, wpm_words)
    target_words = max(words + 8, min(2600, target_words))
    min_words = max(
        words + max(5, math.ceil(words * 0.03)),
        math.ceil(target_words * 0.97),
    )
    max_words = max(min_words + 10, math.ceil(target_words * 1.04))
    return min_words, max_words, target_words
