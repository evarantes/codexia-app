from __future__ import annotations

import os
import unittest

from app.services.visual_quality_rollout import apply_visual_quality_observe_rollout


class VisualQualityObserveRolloutTests(unittest.TestCase):
    def tearDown(self):
        for key in [
            "ENABLE_VISUAL_CRITIC_AI",
            "ENABLE_STRICT_VISUAL_REJECT",
            "VISUAL_QA_FAIL_CLOSED",
            "VISUAL_QA_MODEL",
        ]:
            os.environ.pop(key, None)

    def test_default_rollout_enables_only_ai_observation(self):
        state = apply_visual_quality_observe_rollout()
        self.assertTrue(state["ai_critic_enabled"])
        self.assertFalse(state["strict_visual_reject"])
        self.assertFalse(state["fail_closed"])
        self.assertEqual(state["model"], "gpt-4.1-mini")
        self.assertEqual(os.environ.get("ENABLE_VISUAL_CRITIC_AI"), "true")
        self.assertNotIn("ENABLE_STRICT_VISUAL_REJECT", os.environ)
        self.assertNotIn("VISUAL_QA_FAIL_CLOSED", os.environ)

    def test_explicit_disable_is_respected(self):
        os.environ["ENABLE_VISUAL_CRITIC_AI"] = "false"
        state = apply_visual_quality_observe_rollout()
        self.assertFalse(state["ai_critic_enabled"])
        self.assertEqual(os.environ.get("ENABLE_VISUAL_CRITIC_AI"), "false")

    def test_rollout_never_forces_strict_or_fail_closed(self):
        os.environ["ENABLE_STRICT_VISUAL_REJECT"] = "false"
        os.environ["VISUAL_QA_FAIL_CLOSED"] = "false"
        state = apply_visual_quality_observe_rollout()
        self.assertFalse(state["strict_visual_reject"])
        self.assertFalse(state["fail_closed"])


if __name__ == "__main__":
    unittest.main()
