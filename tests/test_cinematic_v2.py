import os
import unittest
from unittest.mock import patch

from app.services.cinematic_compositor import CinematicCompositor
from app.services.cinematic_director import CinematicDirector
from app.services.cinematic_video_provider import CinematicVideoProvider


class CinematicDirectorTests(unittest.TestCase):
    def test_story_profile_caps_motion_and_budget(self):
        profile = CinematicDirector._profile("story", 10, 999)
        self.assertEqual(profile["kind"], "story")
        self.assertAlmostEqual(profile["target_motion_ratio"], 0.28)
        self.assertLessEqual(profile["default_budget_brl"], 120.0)

    def test_devotional_profile_is_economical(self):
        profile = CinematicDirector._profile("devotional", 10, 30)
        self.assertEqual(profile["kind"], "devotional")
        self.assertAlmostEqual(profile["target_motion_ratio"], 0.10)
        self.assertLessEqual(profile["default_budget_brl"], 45.0)

    def test_current_sonnet_5_pricing_is_used_by_guard(self):
        with patch.dict(os.environ, {}, clear=True):
            cost = CinematicDirector._estimate_cost_usd(
                {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
            )
        self.assertAlmostEqual(cost, 12.0)

    def test_normalize_plan_forces_tier_c_to_still_and_caps_motion(self):
        plan = {
            "scenes": [
                {"tier": "A", "target_seconds": 12, "generative_video_seconds": 15, "recommended_provider": "kling"},
                {"tier": "C", "target_seconds": 12, "generative_video_seconds": 15, "recommended_provider": "veo"},
                {"tier": "B", "target_seconds": 12, "generative_video_seconds": 15, "recommended_provider": "runway"},
            ]
        }
        normalized = CinematicDirector._normalize_plan(
            plan,
            content_type="story",
            duration_minutes=1,
            budget_brl=85,
        )
        self.assertEqual(normalized["motion_budget_seconds"], 17)
        self.assertLessEqual(normalized["motion_planned_seconds"], 17)
        self.assertEqual(normalized["scenes"][1]["tier"], "C")
        self.assertEqual(normalized["scenes"][1]["generative_video_seconds"], 0)
        self.assertEqual(normalized["scenes"][1]["recommended_provider"], "still")


class CinematicCompositorTests(unittest.TestCase):
    def test_scene_windows_ignore_opening_and_endcard(self):
        report = {
            "scene_timeline": [
                {"kind": "opening", "start": 0, "end": 4},
                {"kind": "scene", "scene_index": 1, "visual_start": 4, "visual_end": 13},
                {"kind": "scene", "scene_index": 2, "start": 13, "duration": 8},
                {"kind": "endcard", "start": 21, "end": 25},
            ]
        }
        windows = CinematicCompositor.scene_windows_from_render_report(report)
        self.assertEqual([w["scene_index"] for w in windows], [1, 2])
        self.assertEqual(windows[0]["start"], 4.0)
        self.assertEqual(windows[0]["end"], 13.0)
        self.assertEqual(windows[1]["end"], 21.0)


class CinematicProviderTests(unittest.TestCase):
    def test_status_does_not_call_network_and_handles_missing_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = CinematicVideoProvider(settings=None)
            status = provider.status()
        self.assertIn("runway", status)
        self.assertIn("veo", status)
        self.assertIn("kling", status)
        self.assertFalse(status["runway"]["configured"])
        self.assertFalse(status["veo"]["configured"])
        self.assertFalse(status["kling"]["configured"])


if __name__ == "__main__":
    unittest.main()
