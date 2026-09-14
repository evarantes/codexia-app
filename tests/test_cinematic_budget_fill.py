import unittest

from app.routers.cinematic_budget_optimizer import _cost_brl, _fill_motion_headroom


class CinematicBudgetFillTests(unittest.TestCase):
    def test_story_uses_safe_headroom_to_restore_motion(self):
        scenes = []
        for index in range(1, 5):
            scenes.append(
                {
                    "index": index,
                    "tier": "A",
                    "purpose": "hook" if index == 1 else "climax" if index == 4 else "tension",
                    "recommended_provider": "veo",
                    "target_seconds": 12,
                    "generative_video_seconds": 8,
                }
            )
        for index in range(5, 9):
            scenes.append(
                {
                    "index": index,
                    "tier": "B",
                    "purpose": "story",
                    "recommended_provider": "runway",
                    "target_seconds": 12,
                    "generative_video_seconds": 5,
                }
            )
        for index in range(9, 31):
            scenes.append(
                {
                    "index": index,
                    "tier": "C",
                    "purpose": "story",
                    "recommended_provider": "still",
                    "target_seconds": 12,
                    "generative_video_seconds": 0,
                }
            )

        plan = {
            "scenes": scenes,
            "motion_budget_seconds": 168,
            "motion_planned_seconds": 52,
            "budget_guard": {"enabled": True, "budget_limit_brl": 85.0},
        }
        result = _fill_motion_headroom(
            plan,
            content_type="story",
            duration_minutes=10,
            budget_brl=85.0,
            fx=5.12,
        )
        guard = result["budget_guard"]
        self.assertEqual(guard["quality_target_motion_seconds"], 151)
        self.assertGreaterEqual(result["motion_planned_seconds"], 145)
        self.assertLessEqual(result["motion_planned_seconds"], 168)
        self.assertGreater(guard["motion_fill_added_seconds"], 0)
        self.assertLessEqual(guard["estimated_brl"], 80.75 + 0.05)
        self.assertTrue(guard["budget_respected"])
        self.assertTrue(any(s["tier"] == "B" and s["index"] >= 9 for s in result["scenes"]))

    def test_devotional_stays_restrained(self):
        scenes = [
            {
                "index": index,
                "tier": "C",
                "purpose": "application" if index < 18 else "prayer",
                "recommended_provider": "still",
                "target_seconds": 12,
                "generative_video_seconds": 0,
            }
            for index in range(1, 21)
        ]
        plan = {
            "scenes": scenes,
            "motion_budget_seconds": 60,
            "motion_planned_seconds": 0,
            "budget_guard": {"enabled": True, "budget_limit_brl": 25.0},
        }
        result = _fill_motion_headroom(
            plan,
            content_type="devotional",
            duration_minutes=10,
            budget_brl=25.0,
            fx=5.12,
        )
        self.assertLessEqual(result["motion_planned_seconds"], 45)
        self.assertLessEqual(result["budget_guard"]["estimated_brl"], 23.75 + 0.05)
        self.assertTrue(result["budget_guard"]["budget_respected"])

    def test_cost_guard_math_matches_expected_direction(self):
        base_scenes = [
            {"tier": "B", "generative_video_seconds": 60},
            {"tier": "C", "generative_video_seconds": 0},
        ]
        premium_scenes = [
            {"tier": "A", "generative_video_seconds": 60},
            {"tier": "C", "generative_video_seconds": 0},
        ]
        economy = _cost_brl(scenes=base_scenes, content_type="story", duration_minutes=10, fx=5.12)
        premium = _cost_brl(scenes=premium_scenes, content_type="story", duration_minutes=10, fx=5.12)
        self.assertGreater(premium, economy)


if __name__ == "__main__":
    unittest.main()
