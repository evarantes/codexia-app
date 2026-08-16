from __future__ import annotations

import os
import unittest

from app.services.visual_quality_rollout import apply_visual_quality_observe_rollout


class VisualQualityObserveRolloutTests(unittest.TestCase):
    def tearDown(self):
        for key in [
            "ENABLE_VISUAL_CRITIC_AI",
            "ENABLE_STRICT_VISUAL_REJECT",
            "VISUAL_QA_MAX_RETRIES",
            "VISUAL_QA_FAIL_CLOSED",
            "VISUAL_QA_MODEL",
        ]:
            os.environ.pop(key, None)

    def test_default_rollout_enables_ai_and_one_selective_retry_fail_open(self):
        state = apply_visual_quality_observe_rollout()
        self.assertTrue(state["ai_critic_enabled"])
        self.assertTrue(state["strict_visual_reject"])
        self.assertFalse(state["fail_closed"])
        self.assertEqual(state["max_retries"], 1)
        self.assertEqual(state["model"], "gpt-4.1-mini")
        self.assertEqual(os.environ.get("ENABLE_VISUAL_CRITIC_AI"), "true")
        self.assertEqual(os.environ.get("ENABLE_STRICT_VISUAL_REJECT"), "true")
        self.assertEqual(os.environ.get("VISUAL_QA_MAX_RETRIES"), "1")
        self.assertEqual(os.environ.get("VISUAL_QA_FAIL_CLOSED"), "false")

    def test_explicit_disable_is_respected(self):
        os.environ["ENABLE_VISUAL_CRITIC_AI"] = "false"
        os.environ["ENABLE_STRICT_VISUAL_REJECT"] = "false"
        os.environ["VISUAL_QA_MAX_RETRIES"] = "0"
        state = apply_visual_quality_observe_rollout()
        self.assertFalse(state["ai_critic_enabled"])
        self.assertFalse(state["strict_visual_reject"])
        self.assertEqual(state["max_retries"], 0)
        self.assertEqual(os.environ.get("ENABLE_VISUAL_CRITIC_AI"), "false")
        self.assertEqual(os.environ.get("ENABLE_STRICT_VISUAL_REJECT"), "false")
        self.assertEqual(os.environ.get("VISUAL_QA_MAX_RETRIES"), "0")

    def test_rollout_never_forces_fail_closed_and_respects_strict_override(self):
        os.environ["ENABLE_STRICT_VISUAL_REJECT"] = "false"
        os.environ["VISUAL_QA_FAIL_CLOSED"] = "false"
        state = apply_visual_quality_observe_rollout()
        self.assertFalse(state["strict_visual_reject"])
        self.assertFalse(state["fail_closed"])

    def test_retry_limit_is_clamped_to_one(self):
        os.environ["VISUAL_QA_MAX_RETRIES"] = "3"
        state = apply_visual_quality_observe_rollout()
        self.assertEqual(state["max_retries"], 1)


if __name__ == "__main__":
    unittest.main()
