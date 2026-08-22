from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.channel_excellence_guard import _quality_gate, install_channel_excellence_guard_patch
from app.services.recovery_image_budget import (
    RecoveryImageBudgetExceeded,
    RecoveryImageCallBudget,
    resolve_recovery_image_budget,
)
from app.services.video_generator import VideoGenerator


def _recovery_plan(*, expected: int = 10, existing: int = 0, missing: int = 10):
    return {
        "title": "Recuperação controlada",
        "_partial_image_recovery": {
            "enabled": True,
            "expected_image_count": expected,
            "existing_image_count": existing,
            "missing_image_count": missing,
            "estimated_image_cost_usd": 0.62,
            "estimated_image_cost_brl": 3.22,
            "plan_hash": "confirmed-plan",
        },
    }


class _SuccessfulImageService:
    def __init__(self, image_path: str):
        self.image_path = image_path
        self.calls = 0

    def generate_image(self, *_args, **_kwargs):
        self.calls += 1
        return self.image_path


class RecoveryImageBudgetCapTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ENABLE_STRICT_VISUAL_UNIQUENESS", None)

    def test_budget_clamps_inconsistent_missing_count_and_cost(self):
        state = resolve_recovery_image_budget(
            _recovery_plan(expected=10, existing=7, missing=9)
        )

        self.assertTrue(state["enabled"])
        self.assertEqual(state["allowed_new_image_calls"], 3)
        self.assertEqual(state["remaining_new_image_calls"], 3)
        self.assertAlmostEqual(state["confirmed_max_image_cost_usd"], 0.206667, places=6)
        self.assertEqual(state["confirmed_max_image_cost_brl"], 1.07)

    def test_provider_is_never_called_after_confirmed_ten_image_limit(self):
        statuses = []
        with tempfile.TemporaryDirectory(prefix="recovery-budget-") as tmp:
            image_path = Path(tmp) / "generated.png"
            image_path.write_bytes(b"x" * 2048)
            fake = _SuccessfulImageService(str(image_path))
            generator = VideoGenerator(output_dir=tmp, ai_service=fake)
            budget = RecoveryImageCallBudget(_recovery_plan())

            for index in range(10):
                result = generator._ensure_image_for_scene(
                    f"Cena cinematográfica {index + 1}",
                    "Narração aprovada",
                    status_callback=statuses.append,
                    paid_call_guard=budget.consume,
                )
                self.assertEqual(result, str(image_path))

            with self.assertRaisesRegex(RecoveryImageBudgetExceeded, "10/10"):
                generator._ensure_image_for_scene(
                    "Cena cinematográfica 11",
                    "Narração aprovada",
                    status_callback=statuses.append,
                    paid_call_guard=budget.consume,
                )

        self.assertEqual(fake.calls, 10)
        self.assertTrue(any("imagem paga 10/10" in message for message in statuses))
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["used_new_image_calls"], 10)
        self.assertEqual(snapshot["remaining_new_image_calls"], 0)
        self.assertEqual(snapshot["estimated_consumed_image_cost_usd"], 0.62)
        self.assertEqual(snapshot["estimated_consumed_image_cost_brl"], 3.22)

    def test_strict_uniqueness_does_not_override_confirmed_recovery_target(self):
        os.environ["ENABLE_STRICT_VISUAL_UNIQUENESS"] = "true"

        class RecoveryGenerator(VideoGenerator):
            _codexia_channel_excellence_guard_installed = False

        install_channel_excellence_guard_patch(RecoveryGenerator)
        generator = object.__new__(RecoveryGenerator)
        scenes = [
            {"text": f"Cena {index + 1}", "_estimated_narration_sec": 12.5}
            for index in range(48)
        ]

        self.assertEqual(generator._target_visual_count(scenes, _recovery_plan()), 10)
        self.assertEqual(generator._target_visual_count(scenes, {}), 48)

        grouped = generator._build_visual_groups(scenes, _recovery_plan())
        group_sizes = [len(group["scene_indexes"]) for group in grouped["groups"]]
        self.assertEqual(grouped["target_image_count"], 10)
        self.assertEqual(len(group_sizes), 10)
        self.assertLessEqual(max(group_sizes), 5)
        self.assertGreaterEqual(min(group_sizes), 3)

    def test_quality_gate_accepts_declared_reuse_only_within_confirmed_budget(self):
        plan = _recovery_plan()
        result = {
            "render_report": {
                "narration_plan": {"opening_text": "Título aprovado.", "closing_text": ""},
                "visual_plan": {
                    "generated_image_count": 10,
                    "reused_image_count": 38,
                    "average_image_duration_sec": 60.0,
                    "recovery_image_budget": {
                        "used_new_image_calls": 10,
                        "allowed_new_image_calls": 10,
                        "confirmed_max_image_cost_usd": 0.62,
                        "confirmed_max_image_cost_brl": 3.22,
                        "estimated_consumed_image_cost_usd": 0.62,
                        "estimated_consumed_image_cost_brl": 3.22,
                    },
                },
            }
        }

        accepted = _quality_gate(result, plan=plan)
        self.assertTrue(accepted["passed"])
        self.assertTrue(accepted["budget_limited_visuals"])
        self.assertTrue(accepted["checks"]["confirmed_recovery_image_budget_respected"])
        self.assertEqual(accepted["metrics"]["paid_image_calls_used"], 10)

        result["render_report"]["visual_plan"]["recovery_image_budget"]["used_new_image_calls"] = 11
        rejected = _quality_gate(result, plan=plan)
        self.assertFalse(rejected["passed"])
        self.assertIn("confirmed_recovery_image_budget_exceeded", rejected["violations"])

    def test_all_renderer_paid_image_paths_share_the_same_guard(self):
        source = Path("app/services/video_generator.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("paid_call_guard=paid_image_call_guard"), 4)


if __name__ == "__main__":
    unittest.main()
