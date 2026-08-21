from __future__ import annotations

import unittest
from pathlib import Path

from app.routers.youtube import (
    _recovery_audio_duration_plausible,
    _recovery_collect_visual_candidates,
)


ROOT = Path(__file__).resolve().parents[1]


class RecoveryCheckpointHardeningTests(unittest.TestCase):
    def test_55_second_audio_is_never_valid_for_ten_minute_retry(self):
        self.assertFalse(_recovery_audio_duration_plausible(55.338, 10))
        self.assertTrue(_recovery_audio_duration_plausible(600.0, 10))
        self.assertTrue(_recovery_audio_duration_plausible(774.0, 10))
        self.assertFalse(_recovery_audio_duration_plausible(1200.0, 10))

    def test_visuals_are_recovered_from_render_report_scene_visuals(self):
        result = {
            "script": {"scenes": [{"text": "Cena válida"}]},
            "render_report": {
                "scene_visuals": [
                    {"scene_number": 1, "image_path": "/data/generated/a.png"},
                    {"scene_number": 2, "image_path": "/data/generated/b.png"},
                    {"scene_number": 3, "image_path": "/data/generated/a.png"},
                ]
            },
        }
        self.assertEqual(
            _recovery_collect_visual_candidates(result),
            ["/data/generated/a.png", "/data/generated/b.png"],
        )

    def test_visuals_are_recovered_from_unified_images_and_storyboard_shape(self):
        unified_shape = {
            "selected_images": [
                "/data/unified/images/one.png",
                "/data/unified/images/two.png",
            ],
            "storyboard": {
                "scenes": [
                    {"image_path": "/data/unified/images/three.png"},
                    {"image_path": "/data/unified/images/one.png"},
                ]
            },
        }
        self.assertEqual(
            _recovery_collect_visual_candidates(unified_shape),
            [
                "/data/unified/images/one.png",
                "/data/unified/images/two.png",
                "/data/unified/images/three.png",
            ],
        )

    def test_runtime_build_applies_checkpoint_v3_and_blocks_silent_paid_retry(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_RECOVERY_CHECKPOINT_V3_START", router)
        self.assertNotIn("CODEXIA_RECOVERY_CHECKPOINT_V2_START", router)
        self.assertIn('"strategy": "highest_valid_checkpoint_v3"', router)
        self.assertIn('payload["seeded_script"] = seed_script', router)
        self.assertIn('payload["selected_images"] = list(valid_images)', router)
        self.assertIn('payload["reuse_audio_from"] = dict(audio_generation)', router)
        self.assertIn('payload["force_render_only"] = bool(render_only)', router)
        self.assertIn('payload["_recovery_block_paid_regeneration"] = True', router)
        self.assertIn("db.query(UnifiedVideo)", router)
        self.assertIn("Nenhuma nova mídia foi gerada nesta tentativa.", router)

        for filename in ("Dockerfile", "Dockerfile.worker"):
            content = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("apply_recovery_checkpoint_hardening.py --apply", content)
            self.assertIn("apply_recovery_checkpoint_hardening.py --check", content)


if __name__ == "__main__":
    unittest.main()
