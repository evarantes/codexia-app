from __future__ import annotations

import unittest
from pathlib import Path

from app.services.channel_excellence_guard import _quality_gate


class FinalVisualQualityGateSelfHealTests(unittest.TestCase):
    def _result(self, *, reused=1, legacy_avg=14.0, beat_hold=16.6, target=16.7, opening="Jesus é o centro da nossa fé"):
        return {
            "render_report": {
                "narration_plan": {
                    "opening_text": opening,
                    "closing_text": "",
                },
                "visual_plan": {
                    "generated_image_count": 48,
                    "reused_image_count": reused,
                    "average_image_duration_sec": legacy_avg,
                },
                "resource_profile": {
                    "visual_hold_target_sec": target,
                },
                "scene_visuals": [
                    {
                        "scene_number": 1,
                        "max_visual_hold_sec": beat_hold,
                        "visual_beat_count": 2,
                        "visual_beat_effects": ["push_in", "pan_left"],
                    },
                    {
                        "scene_number": 2,
                        "max_visual_hold_sec": min(beat_hold, target),
                        "visual_beat_count": 2,
                        "visual_beat_effects": ["push_out", "pan_right"],
                    },
                ],
            }
        }

    def test_reused_path_is_review_warning_not_render_failure(self):
        report = _quality_gate(self._result(), plan={"scenes": [{"text": "a"}, {"text": "b"}]})

        self.assertTrue(report["passed"])
        self.assertIn("generated_image_path_reused", report["warnings"])
        self.assertNotIn("generated_image_path_reused", report["blocking_violations"])
        self.assertTrue(report["review_recommended"])
        self.assertTrue(report["auto_render_preserved"])

    def test_hold_uses_real_cinematic_beat_not_legacy_asset_total(self):
        report = _quality_gate(
            self._result(reused=0, legacy_avg=28.0, beat_hold=16.6, target=16.7),
            plan={"scenes": [{"text": "a"}, {"text": "b"}]},
        )

        self.assertTrue(report["passed"])
        self.assertNotIn("visual_hold_too_long", report["warnings"])
        self.assertEqual(report["metrics"]["legacy_average_image_duration_sec"], 28.0)
        self.assertEqual(report["metrics"]["max_visual_beat_hold_sec"], 16.6)
        self.assertGreater(report["metrics"]["visual_hold_limit_sec"], 16.6)

    def test_real_excessive_hold_warns_but_keeps_valid_render_for_review(self):
        report = _quality_gate(
            self._result(reused=0, beat_hold=21.0, target=16.7),
            plan={"scenes": [{"text": "a"}, {"text": "b"}]},
        )

        self.assertTrue(report["passed"])
        self.assertIn("visual_hold_too_long", report["warnings"])
        self.assertFalse(report["blocking_violations"])
        self.assertTrue(report["review_recommended"])

    def test_non_visual_quality_problem_remains_blocking(self):
        report = _quality_gate(
            self._result(reused=0, beat_hold=7.0, target=7.0, opening="Uma mensagem de fé para você"),
            plan={"scenes": [{"text": "a"}, {"text": "b"}]},
        )

        self.assertFalse(report["passed"])
        self.assertIn("generic_automatic_opening", report["blocking_violations"])

    def test_built_source_contains_self_heal_marker(self):
        source = Path("app/services/channel_excellence_guard.py").read_text(encoding="utf-8")
        self.assertIn("final_visual_quality_gate_self_heal_v1", source)
        self.assertNotIn("pacing_ok = avg_hold <= 11.0", source)


if __name__ == "__main__":
    unittest.main()
