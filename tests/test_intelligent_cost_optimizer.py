import unittest

from app.services.intelligent_cost_optimizer import (
    build_sparse_visual_optimization_plan,
    proportional_visual_index,
    validate_optimization_confirmation,
)
from scripts import apply_intelligent_cost_optimization as optimizer_patch
from scripts import apply_intelligent_cost_optimization_compat as optimizer_compat


class IntelligentCostOptimizerTests(unittest.TestCase):
    def _plan(self, **overrides):
        kwargs = {
            "task_id": "task-45",
            "title": "O Pastor Que Conhece Cada Passo Seu",
            "target_visual_count": 38,
            "valid_image_paths": [f"/data/media/images/img-{idx:02d}.png" for idx in range(31)],
            "script": {
                "title": "O Pastor Que Conhece Cada Passo Seu",
                "scenes": [
                    {"narration": "Primeiro trecho da mensagem."},
                    {"narration": "Segundo trecho da mensagem."},
                ],
            },
            "audio_path": "/data/media/audio/task-45.mp3",
            "image_unit_cost_usd": 0.04,
        }
        kwargs.update(overrides)
        return build_sparse_visual_optimization_plan(**kwargs)

    def test_sparse_plan_requires_explicit_confirmation_and_zero_paid_images(self):
        plan = self._plan()
        self.assertTrue(plan["optimization_required"])
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(plan["valid_image_count"], 31)
        self.assertEqual(plan["target_visual_count"], 38)
        self.assertEqual(plan["missing_visual_count"], 7)
        self.assertEqual(plan["estimated_image_calls_avoided"], 7)
        self.assertEqual(plan["paid_image_calls"], 0)
        self.assertAlmostEqual(plan["estimated_savings_usd"], 0.28, places=6)
        self.assertTrue(plan["preserve_full_script"])
        self.assertTrue(plan["preserve_full_narration"])
        self.assertTrue(plan["quality_policy"]["never_shorten_narration"])
        self.assertTrue(plan["quality_policy"]["never_remove_script_text"])
        self.assertTrue(plan["quality_policy"]["never_generate_paid_images"])

    def test_confirmation_hash_must_match_exact_plan(self):
        plan = self._plan()
        self.assertTrue(validate_optimization_confirmation(plan, plan["plan_hash"]))
        self.assertFalse(validate_optimization_confirmation(plan, "wrong-hash"))
        self.assertFalse(validate_optimization_confirmation(plan, ""))

    def test_script_or_asset_change_invalidates_old_confirmation(self):
        original = self._plan()
        changed_script = self._plan(
            script={
                "title": "O Pastor Que Conhece Cada Passo Seu",
                "scenes": [{"narration": "Mensagem alterada depois da proposta."}],
            }
        )
        changed_images = self._plan(
            valid_image_paths=[f"/data/media/images/img-{idx:02d}.png" for idx in range(30)]
        )
        self.assertNotEqual(original["plan_hash"], changed_script["plan_hash"])
        self.assertNotEqual(original["plan_hash"], changed_images["plan_hash"])
        self.assertFalse(validate_optimization_confirmation(changed_script, original["plan_hash"]))
        self.assertFalse(validate_optimization_confirmation(changed_images, original["plan_hash"]))

    def test_full_visual_set_needs_no_strategy_change_confirmation(self):
        plan = self._plan(
            valid_image_paths=[f"/data/media/images/img-{idx:02d}.png" for idx in range(38)]
        )
        self.assertFalse(plan["optimization_required"])
        self.assertFalse(plan["requires_confirmation"])
        self.assertEqual(plan["missing_visual_count"], 0)
        self.assertEqual(plan["estimated_image_calls_avoided"], 0)

    def test_proportional_visual_mapping_is_monotonic_and_uses_entire_pool(self):
        indexes = [proportional_visual_index(group, 31, 48) for group in range(48)]
        self.assertEqual(indexes[0], 0)
        self.assertEqual(indexes[-1], 30)
        self.assertEqual(sorted(indexes), indexes)
        self.assertTrue(all((b - a) in (0, 1) for a, b in zip(indexes, indexes[1:])))
        self.assertEqual(set(indexes), set(range(31)))

    def test_mapping_does_not_round_robin_or_freeze_last_image_early(self):
        indexes = [proportional_visual_index(group, 3, 8) for group in range(8)]
        self.assertEqual(indexes, [0, 0, 0, 1, 1, 1, 2, 2])
        self.assertNotEqual(indexes, [0, 1, 2, 2, 2, 2, 2, 2])

    def test_compat_protects_every_equivalent_retry_path(self):
        legacy = optimizer_patch.UI_RETRY_OLD
        html = f"<script>\n{legacy}\n// segundo caminho\n{legacy}\n</script>"
        patched = optimizer_compat._patch_index_all(html)

        self.assertNotIn(legacy, patched)
        self.assertEqual(patched.count("retry-plan"), 2)
        self.assertEqual(patched.count("optimization_plan_hash"), 2)
        self.assertEqual(patched.count(optimizer_patch.MARKER), 1)
        self.assertEqual(optimizer_compat._patch_index_all(patched), patched)


if __name__ == "__main__":
    unittest.main()
