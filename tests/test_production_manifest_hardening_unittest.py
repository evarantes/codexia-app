from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import production_manifest as pm


ROOT = Path(__file__).resolve().parents[1]


class ProductionManifestHardeningTests(unittest.TestCase):
    def test_partial_recovery_payload_marks_seed_script_and_reuses_audio(self):
        manifest = {
            "script": {
                "title": "Teste",
                "scenes": [
                    {"text": "Cena 1", "image_prompt": "Prompt 1"},
                    {"text": "Cena 2", "image_prompt": "Prompt 2"},
                    {"text": "Cena 3", "image_prompt": "Prompt 3"},
                ],
            }
        }
        plan = {
            "action": "regenerate_missing_images",
            "plan_hash": "plan-123",
            "existing_image_paths": ["/data/media/images/existing-1.png"],
            "audio_path": "/data/media/audio/voice.mp3",
            "audio_duration_sec": 600.0,
            "expected_image_count": 3,
            "missing_image_count": 2,
        }
        with patch.object(pm, "load_manifest", return_value=manifest):
            payload = pm.recovery_payload_patch("task-1", {"duration": 10}, plan)

        self.assertTrue(payload["force_reuse_assets"])
        self.assertFalse(payload["force_render_only"])
        self.assertEqual(payload["selected_images"], ["/data/media/images/existing-1.png"])
        self.assertEqual(payload["reuse_audio_from"]["output_path"], "/data/media/audio/voice.mp3")
        meta = payload["seeded_script"]["_partial_image_recovery"]
        self.assertTrue(meta["enabled"])
        self.assertEqual(meta["existing_image_count"], 1)
        self.assertEqual(meta["expected_image_count"], 3)
        self.assertEqual(meta["missing_image_count"], 2)
        self.assertEqual(meta["plan_hash"], "plan-123")

    def test_video_generator_keeps_full_group_plan_and_only_fills_missing_groups(self):
        source = (ROOT / "app/services/video_generator.py").read_text(encoding="utf-8")
        self.assertIn("# CODEXIA_PARTIAL_IMAGE_RECOVERY_V1", source)
        self.assertIn(
            "selected_image_count=(0 if partial_image_recovery else len(selected_image_paths))",
            source,
        )
        self.assertIn("visual_group_id < len(selected_image_paths)", source)
        self.assertIn("selected_primary_path if partial_image_recovery", source)

    def test_router_uses_manifest_audio_and_requires_paid_confirmation(self):
        source = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn('getattr(request, "reuse_audio_from", None)', source)
        self.assertIn('script["_partial_image_recovery"] = dict(partial_meta)', source)
        self.assertIn('manifest_action == "regenerate_missing_images"', source)
        self.assertIn('manifest_action == "rerender_without_paid_media"', source)
        self.assertIn("confirm_or_prepare_partial_recovery(task_id, payload)", source)

    def test_task_manager_syncs_manifest_before_redis(self):
        source = (ROOT / "app/services/task_manager.py").read_text(encoding="utf-8")
        marker_pos = source.find("CODEXIA_PRODUCTION_MANIFEST_TASK_SYNC_V1")
        redis_pos = source.find("_redis_conn.set(_REDIS_PREFIX + task_id", marker_pos)
        self.assertGreaterEqual(marker_pos, 0)
        self.assertGreater(redis_pos, marker_pos)


if __name__ == "__main__":
    unittest.main()
