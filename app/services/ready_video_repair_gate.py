from __future__ import annotations

from typing import Any, Dict, Optional


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _nonnegative_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def confirmed_ready_video_repair_budget(payload: Any) -> bool:
    """Return True only for the exact paid budget confirmed by ready-video repair.

    This gate intentionally does not authorize arbitrary recovery payloads.  It
    accepts only the authenticated ``Corrigir com ativos`` shape, where the
    top-level repair budget and the copy persisted inside ``seeded_script`` are
    identical.  Any missing flag, changed hash or changed count fails closed so
    the legacy paid-recovery guard remains active.
    """

    data = _as_dict(payload)
    if not (
        bool(data.get("repair_mode"))
        and bool(data.get("repair_complete_visuals"))
        and bool(data.get("repair_regenerate_audio"))
        and bool(data.get("repair_exclude_video"))
    ):
        return False

    source_video_id = _nonnegative_int(data.get("repair_source_scheduled_video_id"))
    if source_video_id is None or source_video_id <= 0:
        return False

    confirmed = _as_dict(data.get("repair_image_budget"))
    seeded = _as_dict(data.get("seeded_script"))
    partial = _as_dict(seeded.get("_partial_image_recovery"))
    if not (bool(confirmed.get("enabled")) and bool(partial.get("enabled"))):
        return False

    confirmed_hash = str(confirmed.get("plan_hash") or "").strip()
    partial_hash = str(partial.get("plan_hash") or "").strip()
    if not confirmed_hash or confirmed_hash != partial_hash or not confirmed_hash.startswith("repair:"):
        return False

    values: Dict[str, int] = {}
    for key in ("expected_image_count", "existing_image_count", "missing_image_count"):
        confirmed_value = _nonnegative_int(confirmed.get(key))
        partial_value = _nonnegative_int(partial.get(key))
        if confirmed_value is None or partial_value is None or confirmed_value != partial_value:
            return False
        values[key] = confirmed_value

    expected = values["expected_image_count"]
    existing = values["existing_image_count"]
    missing = values["missing_image_count"]
    if expected <= 0 or existing > expected or existing + missing != expected:
        return False

    return True


__all__ = ["confirmed_ready_video_repair_budget"]
