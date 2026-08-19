from __future__ import annotations

import os
import unittest
from pathlib import Path

from app.routers.video_costs import build_cost_estimate_payload
from app.services.video_cost_estimator import estimate_video_cost


class VideoCostControlTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "CODEXIA_USD_BRL",
            "CODEXIA_ECONOMY_IMAGE_COST_USD",
            "CODEXIA_BALANCED_IMAGE_COST_USD",
            "CODEXIA_PREMIUM_IMAGE_COST_USD",
        ):
            os.environ.pop(key, None)

    def test_estimate_returns_brl_and_standard_projections(self):
        os.environ["CODEXIA_USD_BRL"] = "5.00"
        data = build_cost_estimate_payload(2, mode="balanced")
        self.assertEqual(data["provider"], "openai")
        self.assertEqual(data["model"], "gpt-image-2")
        self.assertEqual(data["estimated_images"], 16)
        self.assertEqual(data["usd_brl"], 5.0)
        self.assertAlmostEqual(data["total_cost_brl"], data["total_cost_usd"] * 5.0, places=2)
        self.assertEqual([p["duration_minutes"] for p in data["projections"]], [2.0, 5.0, 10.0, 15.0])

    def test_budget_profiles_are_ordered_by_cost(self):
        economy = estimate_video_cost(2, mode="economy", regeneration_rate=0)
        balanced = estimate_video_cost(2, mode="balanced", regeneration_rate=0)
        premium = estimate_video_cost(2, mode="premium", regeneration_rate=0)
        self.assertLess(economy.total_cost_usd, balanced.total_cost_usd)
        self.assertLess(balanced.total_cost_usd, premium.total_cost_usd)

    def test_runtime_contract_files_are_present(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "app/static/video_cost_control.js").exists())
        self.assertTrue((root / "scripts/apply_video_cost_backend_hardening.py").exists())
        self.assertTrue((root / "scripts/apply_video_cost_ui_hardening.py").exists())


if __name__ == "__main__":
    unittest.main()
