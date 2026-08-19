from __future__ import annotations

import os
import unittest

from app.services.video_cost_estimator import estimate_video_cost, project_from_baseline


class VideoCostEstimatorTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "CODEXIA_BALANCED_IMAGES_PER_MINUTE",
            "CODEXIA_BALANCED_IMAGE_COST_USD",
            "CODEXIA_VIDEO_FIXED_COST_USD",
            "CODEXIA_OPENAI_IMAGE_MODEL",
            "OPENAI_IMAGE_QUALITY",
        ):
            os.environ.pop(key, None)

    def test_balanced_two_minute_estimate_uses_openai_gpt_image_2(self):
        result = estimate_video_cost(2, mode="balanced")
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.model, "gpt-image-2")
        self.assertEqual(result.image_quality, "medium")
        self.assertEqual(result.estimated_images, 16)
        self.assertEqual(result.estimated_regenerations, 2)
        self.assertEqual(result.estimated_endcards, 1)
        self.assertGreater(result.total_cost_usd, 0)

    def test_cost_estimate_is_configurable_without_code_change(self):
        os.environ["CODEXIA_BALANCED_IMAGES_PER_MINUTE"] = "6"
        os.environ["CODEXIA_BALANCED_IMAGE_COST_USD"] = "0.04"
        os.environ["CODEXIA_VIDEO_FIXED_COST_USD"] = "0.20"
        os.environ["CODEXIA_OPENAI_IMAGE_MODEL"] = "gpt-image-2-2026-04-21"
        os.environ["OPENAI_IMAGE_QUALITY"] = "high"
        result = estimate_video_cost(2, mode="balanced", regeneration_rate=0.0)
        self.assertEqual(result.estimated_images, 12)
        self.assertEqual(result.estimated_regenerations, 0)
        self.assertEqual(result.image_quality, "high")
        self.assertEqual(result.model, "gpt-image-2-2026-04-21")
        self.assertAlmostEqual(result.total_cost_usd, 0.72, places=6)

    def test_projection_separates_fixed_and_variable_cost(self):
        result = project_from_baseline(2, 6.10, 10, fixed_cost_usd=0.10)
        self.assertAlmostEqual(result["variable_cost_per_minute_usd"], 3.0, places=6)
        self.assertAlmostEqual(result["projected_total_cost_usd"], 30.10, places=6)


if __name__ == "__main__":
    unittest.main()
