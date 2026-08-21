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

    def test_runtime_build_applies_checkpoint_hardening_last(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_RECOVERY_CHECKPOINT_V2_START", router)
        self.assertIn('"strategy": "highest_valid_checkpoint"', router)
        self.assertIn('payload["force_render_only"] = render_only', router)

        for filename in ("Dockerfile", "Dockerfile.worker"):
            content = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("apply_recovery_checkpoint_hardening.py --apply", content)
            self.assertIn("apply_recovery_checkpoint_hardening.py --check", content)


if __name__ == "__main__":
    unittest.main()
