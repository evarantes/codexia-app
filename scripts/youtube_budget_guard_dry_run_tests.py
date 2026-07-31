import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.models import Settings
from app.services.financial_guardian.adapters import youtube_auto_financial_adapter
from app.services.financial_guardian_service import FinancialGuardianService


def _get_or_create_settings(db):
    row = db.query(Settings).order_by(Settings.id.desc()).first()
    if row is None:
        row = Settings()
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _run_case(*, label: str, per_video_limit: float):
    db = SessionLocal()
    try:
        s = _get_or_create_settings(db)
        s.per_video_spend_limit = float(per_video_limit)
        s.daily_spend_limit = 0.0
        s.monthly_spend_limit = 0.0
        db.add(s)
        db.commit()

        guardian = FinancialGuardianService()
        payload = {
            "mode": "topic",
            "kind": "story",
            "topic": "Piloto de teste (budget guard dry-run)",
            "duration": 5,
            "image_mode": "multiple",
            "auto_upload": False,
            "aspect_ratio": "16:9",
        }
        context = youtube_auto_financial_adapter.build_context(
            task_id=f"budget-guard-{label}",
            payload=payload,
            user_id=None,
            status="queued",
        )
        decision = guardian.evaluate_context_preflight(
            db,
            context=context,
            config=youtube_auto_financial_adapter.build_guardrail_config(),
            adapter=youtube_auto_financial_adapter,
        )
        db.commit()
        print(
            {
                "case": label,
                "estimated_cost": float(getattr(context, "estimated_cost", 0.0) or 0.0),
                "per_video_limit": float(per_video_limit),
                "allowed": bool(decision.get("allowed")),
                "reason": decision.get("reason"),
            }
        )
    finally:
        db.close()


def main() -> int:
    os.environ["AI_COST_DRY_RUN"] = "1"
    _run_case(label="A_allowed", per_video_limit=0.6)
    _run_case(label="B_blocked", per_video_limit=0.30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

