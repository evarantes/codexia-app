import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.services.financial_guardian.adapters import youtube_auto_financial_adapter
from app.services.financial_guardian_service import FinancialGuardianService


def main() -> int:
    os.environ["AI_COST_DRY_RUN"] = "1"
    db = SessionLocal()
    try:
        guardian = FinancialGuardianService()
        payload = {
            "mode": "topic",
            "kind": "story",
            "topic": "Piloto de teste (somente preflight, sem chamadas pagas)",
            "duration": 5,
            "image_mode": "multiple",
            "auto_upload": False,
            "aspect_ratio": "16:9",
        }
        context = youtube_auto_financial_adapter.build_context(
            task_id="preflight-structural-test",
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
        print({"ok": True, "preflight": decision})
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

