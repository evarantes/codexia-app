from __future__ import annotations

from typing import Any, Dict, Optional


class RecoveryImageBudgetExceeded(RuntimeError):
    """Raised before a paid image call can exceed the confirmed recovery cap."""


def _non_negative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _non_negative_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return max(0.0, float(default))


def resolve_recovery_image_budget(plan: Any) -> Dict[str, Any]:
    """Return the paid-image ceiling embedded in a partial recovery plan.

    The router writes ``_partial_image_recovery`` only after the user confirms
    the exact recovery plan. Its expected/missing counts are therefore an
    execution boundary, not merely an estimate displayed by the UI.
    """
    disabled = {
        "enabled": False,
        "expected_image_count": 0,
        "existing_image_count": 0,
        "allowed_new_image_calls": 0,
        "used_new_image_calls": 0,
        "remaining_new_image_calls": 0,
        "confirmed_max_image_cost_usd": 0.0,
        "confirmed_max_image_cost_brl": 0.0,
        "estimated_consumed_image_cost_usd": 0.0,
        "estimated_consumed_image_cost_brl": 0.0,
        "plan_hash": "",
    }
    if not isinstance(plan, dict):
        return disabled

    partial = plan.get("_partial_image_recovery")
    if not isinstance(partial, dict) or not bool(partial.get("enabled")):
        return disabled

    expected = _non_negative_int(partial.get("expected_image_count"))
    existing = _non_negative_int(partial.get("existing_image_count"))
    declared_missing = _non_negative_int(partial.get("missing_image_count"))
    if expected <= 0:
        expected = existing + declared_missing

    remaining_slots = max(0, expected - min(existing, expected))
    allowed = min(declared_missing, remaining_slots)
    declared_cost_usd = _non_negative_float(partial.get("estimated_image_cost_usd"))
    declared_cost_brl = _non_negative_float(partial.get("estimated_image_cost_brl"))
    allowed_ratio = (float(allowed) / float(declared_missing)) if declared_missing > 0 else 0.0

    return {
        "enabled": True,
        "expected_image_count": expected,
        "existing_image_count": min(existing, expected) if expected else existing,
        "allowed_new_image_calls": allowed,
        "used_new_image_calls": 0,
        "remaining_new_image_calls": allowed,
        "confirmed_max_image_cost_usd": round(declared_cost_usd * allowed_ratio, 6),
        "confirmed_max_image_cost_brl": round(declared_cost_brl * allowed_ratio, 2),
        "estimated_consumed_image_cost_usd": 0.0,
        "estimated_consumed_image_cost_brl": 0.0,
        "plan_hash": str(partial.get("plan_hash") or "").strip(),
    }


class RecoveryImageCallBudget:
    """Guard consumed immediately before every provider image call."""

    def __init__(self, plan: Any):
        self._state = resolve_recovery_image_budget(plan)

    @property
    def enabled(self) -> bool:
        return bool(self._state.get("enabled"))

    @property
    def target_image_count(self) -> Optional[int]:
        if not self.enabled:
            return None
        return _non_negative_int(self._state.get("expected_image_count"))

    def consume(self) -> Dict[str, Any]:
        if not self.enabled:
            return self.snapshot()

        limit = _non_negative_int(self._state.get("allowed_new_image_calls"))
        used = _non_negative_int(self._state.get("used_new_image_calls"))
        if used >= limit:
            raise RecoveryImageBudgetExceeded(
                "Limite rígido da recuperação atingido antes de uma nova chamada paga de imagem: "
                f"{used}/{limit}. Nenhuma chamada adicional foi iniciada."
            )

        self._state["used_new_image_calls"] = used + 1
        self._state["remaining_new_image_calls"] = max(0, limit - used - 1)
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        snapshot = dict(self._state)
        allowed = _non_negative_int(snapshot.get("allowed_new_image_calls"))
        used = min(allowed, _non_negative_int(snapshot.get("used_new_image_calls")))
        used_ratio = (float(used) / float(allowed)) if allowed > 0 else 0.0
        snapshot["estimated_consumed_image_cost_usd"] = round(
            _non_negative_float(snapshot.get("confirmed_max_image_cost_usd")) * used_ratio,
            6,
        )
        snapshot["estimated_consumed_image_cost_brl"] = round(
            _non_negative_float(snapshot.get("confirmed_max_image_cost_brl")) * used_ratio,
            2,
        )
        return snapshot
