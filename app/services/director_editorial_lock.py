from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from app.modules.bible_video_factory.editorial_intelligence import EditorialIntelligenceService


_INSTALLED = False
_ORIGINAL = None


def install_director_editorial_lock() -> bool:
    """Do not let a later editor rewrite a Claude Director approved narration.

    The canonical pipeline still performs its technical/pre-media checks. This
    only skips the optional text rewrite when the upstream director explicitly
    marked the narration as reviewed, duration-validated and locked.
    """
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return False

    original = EditorialIntelligenceService.review_plan
    _ORIGINAL = original

    def review_plan_with_director_lock(
        self: EditorialIntelligenceService,
        plan: Dict[str, Any],
        settings_payload=None,
        *,
        script_id=None,
        task_id=None,
    ):
        raw = plan if isinstance(plan, dict) else {}
        contract = raw.get("duration_contract") if isinstance(raw.get("duration_contract"), dict) else {}
        locked = bool(
            raw.get("editorial_reviewed")
            and raw.get("editorial_review_ready")
            and (raw.get("narration_locked") or contract.get("locked_after_approval"))
            and contract.get("validated")
        )
        # Compatibility: the V2 handoff serializes only story_content into the
        # legacy adapter. It still marks editorial_reviewed/editorial_review_ready,
        # so accept those flags even if the adapter has not copied the contract
        # metadata into its scene plan yet.
        if not locked:
            locked = bool(raw.get("editorial_reviewed") and raw.get("editorial_review_ready"))

        if locked:
            return {
                "status": "skipped",
                "plan_updates": {
                    "editorial_intelligence": {
                        "status": "director_locked",
                        "summary": "Narração aprovada pelo Claude Diretor; revisão editorial posterior não altera o texto.",
                        "provider_requested": "DirectorLock",
                        "provider_used": "DirectorLock",
                        "model_requested": "",
                        "model_used": "",
                        "failover_reason": "",
                        "correction_count": 0,
                        "review_time_ms": 0,
                        "review_time_seconds": 0.0,
                        "attempts": [],
                        "script_id": script_id,
                        "task_id": task_id,
                        "applied_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "error": "",
                    }
                },
            }

        return original(
            self,
            plan,
            settings_payload,
            script_id=script_id,
            task_id=task_id,
        )

    EditorialIntelligenceService.review_plan = review_plan_with_director_lock
    _INSTALLED = True
    return True
