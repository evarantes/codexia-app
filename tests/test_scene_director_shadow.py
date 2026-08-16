from __future__ import annotations

import os
import unittest

from app.services.scene_director_shadow import analyze_scene_plan, install_scene_director_shadow_patch
from app.services.visual_quality_rollout import apply_visual_quality_observe_rollout


class _FakeGenerator:
    def __init__(self):
        self.ai_service = type("AI", (), {"ai_task_id": None})()

    def create_video_from_plan(self, plan, *args, **kwargs):
        return {"file_path": "/tmp/fake.mp4", "render_report": {}}


class SceneDirectorShadowTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "ENABLE_VISUAL_CRITIC_AI",
            "ENABLE_STRICT_VISUAL_REJECT",
            "VISUAL_QA_MAX_RETRIES",
            "VISUAL_QA_FAIL_CLOSED",
        ):
            os.environ.pop(key, None)

    def test_detects_repeated_prompts_without_mutating_plan(self):
        plan = {
            "scenes": [
                {"text": "Uma tempestade se aproxima", "image_prompt": "Jesus consolando uma mulher"},
                {"text": "O barco luta contra as ondas", "image_prompt": "Jesus consolando uma mulher"},
                {"text": "Um farol surge ao longe", "image_prompt": "Jesus caminhando perto do mar"},
            ]
        }
        original = repr(plan)
        report = analyze_scene_plan(plan)
        self.assertEqual(repr(plan), original)
        self.assertEqual(report["mode"], "shadow")
        self.assertFalse(report["blocking"])
        self.assertFalse(report["mutated_plan"])
        self.assertIn([1, 2], report["repeated_prompt_pairs"])
        self.assertTrue(any(item["cue"] == "tempestade" for item in report["missed_symbolic_opportunities"]))
        self.assertTrue(any(item["cue"] == "barco" for item in report["missed_symbolic_opportunities"]))

    def test_patch_only_adds_report_to_result(self):
        class Fake(_FakeGenerator):
            pass

        cls = install_scene_director_shadow_patch(Fake)
        instance = cls()
        plan = {"scenes": [{"text": "Esperança", "image_prompt": "sunrise over hills"}]}
        result = instance.create_video_from_plan(plan)
        self.assertIn("scene_director_shadow", result)
        self.assertIn("scene_director_shadow", result["render_report"])
        self.assertFalse(result["scene_director_shadow"]["mutated_plan"])

    def test_rollout_enables_one_retry_but_keeps_fail_open(self):
        rollout = apply_visual_quality_observe_rollout()
        self.assertTrue(rollout["ai_critic_enabled"])
        self.assertTrue(rollout["strict_visual_reject"])
        self.assertEqual(rollout["max_retries"], 1)
        self.assertFalse(rollout["fail_closed"])
        self.assertEqual(os.environ["VISUAL_QA_MAX_RETRIES"], "1")

    def test_explicit_environment_override_is_respected(self):
        os.environ["ENABLE_STRICT_VISUAL_REJECT"] = "false"
        os.environ["VISUAL_QA_MAX_RETRIES"] = "0"
        rollout = apply_visual_quality_observe_rollout()
        self.assertFalse(rollout["strict_visual_reject"])
        self.assertEqual(rollout["max_retries"], 0)


if __name__ == "__main__":
    unittest.main()
