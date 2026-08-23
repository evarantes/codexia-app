from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.ready_video_repair import (
    build_confirmed_image_budget,
    build_repair_preview,
    ordered_preserved_images,
    required_unique_visuals,
)
from app.services.recovery_image_budget import RecoveryImageBudgetExceeded, RecoveryImageCallBudget
from app.services.video_generator import VideoGenerator


class ReadyVideoAssetRepairTests(unittest.TestCase):
    def _manifest_with_images(self, root: Path, count: int = 10):
        artifacts = []
        refs = []
        for idx in range(count):
            path = root / f"img_{idx:02d}.png"
            path.write_bytes((b"x" * 1600) + bytes([idx % 255]))
            refs.append(str(path))
            artifacts.append({
                "kind": "image",
                "original_path": str(path),
                "durable_path": str(path),
                "exists": True,
            })
        return {
            "expected_duration_minutes": 10,
            "expected_image_count": 48,
            "selected_image_references": refs,
            "artifacts": artifacts,
        }

    def test_ten_minute_48_scene_repair_requires_about_40_visuals(self):
        self.assertEqual(required_unique_visuals(600, 48, 15), 40)

    def test_preview_preserves_ten_images_and_requests_only_missing_thirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest_with_images(root, 10)
            script = {"scenes": [{"text": f"Cena {i}"} for i in range(48)]}
            result = {
                "payload": {"duration": 10, "seeded_script": script},
                "script": script,
            }
            preview = build_repair_preview(
                task_id="task-pastor",
                title="O Pastor Que Conhece Cada Passo Seu",
                task_result=result,
                payload=result["payload"],
                manifest=manifest,
                image_cost_unit=0.04,
                seconds_per_image=15,
            )
            self.assertEqual(preview["scene_count"], 48)
            self.assertEqual(preview["required_unique_image_count"], 40)
            self.assertEqual(preview["existing_image_count"], 10)
            self.assertEqual(preview["missing_image_count"], 30)
            self.assertTrue(preview["regenerate_audio"])
            self.assertFalse(preview["reuse_old_audio"])
            self.assertFalse(preview["reuse_old_mp4"])
            self.assertEqual(len(preview["preserved_images"]), 10)
            self.assertAlmostEqual(preview["estimated_new_image_cost"], 1.2, places=6)

    def test_confirmation_must_match_current_missing_count_exactly(self):
        preview = {
            "task_id": "t1",
            "required_unique_image_count": 40,
            "existing_image_count": 10,
            "missing_image_count": 30,
            "image_cost_unit": 0.04,
        }
        with self.assertRaises(ValueError):
            build_confirmed_image_budget(preview, max_new_images=31)
        with self.assertRaises(ValueError):
            build_confirmed_image_budget(preview, max_new_images=29)
        budget = build_confirmed_image_budget(preview, max_new_images=30)
        self.assertTrue(budget["enabled"])
        self.assertEqual(budget["expected_image_count"], 40)
        self.assertEqual(budget["existing_image_count"], 10)
        self.assertEqual(budget["missing_image_count"], 30)

    def test_paid_guard_blocks_call_31_before_provider(self):
        plan = {
            "_partial_image_recovery": {
                "enabled": True,
                "expected_image_count": 40,
                "existing_image_count": 10,
                "missing_image_count": 30,
                "estimated_image_cost_usd": 1.2,
                "estimated_image_cost_brl": 0.0,
                "plan_hash": "repair:t1:40:10:30",
            }
        }
        guard = RecoveryImageCallBudget(plan)
        for _ in range(30):
            guard.consume()
        self.assertTrue(guard.exhausted)
        with self.assertRaises(RecoveryImageBudgetExceeded):
            guard.consume()

    def test_repair_visual_target_is_not_capped_by_ten_selected_images(self):
        scenes = [
            {"text": f"Cena narrativa {idx}", "_estimated_narration_sec": 12.5}
            for idx in range(48)
        ]
        generator = VideoGenerator(output_dir=tempfile.mkdtemp())
        target_without_budget = generator._target_visual_count(
            scenes,
            {"repair_complete_visuals": True, "target_duration_sec": 600},
            selected_image_count=10,
        )
        self.assertGreater(target_without_budget, 10)

        target_with_confirmed_budget = generator._target_visual_count(
            scenes,
            {
                "repair_complete_visuals": True,
                "target_duration_sec": 600,
                "_partial_image_recovery": {
                    "enabled": True,
                    "expected_image_count": 40,
                    "existing_image_count": 10,
                    "missing_image_count": 30,
                    "estimated_image_cost_usd": 0.0,
                    "estimated_image_cost_brl": 0.0,
                    "plan_hash": "repair:test",
                },
            },
            selected_image_count=10,
        )
        self.assertEqual(target_with_confirmed_budget, 40)

    def test_runtime_patch_contains_audio_mp4_exclusion_and_missing_visual_path(self):
        router = Path("app/routers/youtube.py").read_text(encoding="utf-8")
        generator = Path("app/services/video_generator.py").read_text(encoding="utf-8")
        index = Path("app/static/index.html").read_text(encoding="utf-8")
        self.assertIn('repair_regenerate_audio', router)
        self.assertIn('repair_exclude_video', router)
        self.assertIn('/schedule/{video_id}/repair-with-assets', router)
        self.assertIn('can_use_selected_image', generator)
        self.assertIn('int(visual_group_id or 0) < len(selected_image_paths)', generator)
        self.assertIn('Corrigir com ativos', index)


if __name__ == "__main__":
    unittest.main()
