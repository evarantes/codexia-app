import unittest
from pathlib import Path

from scripts.apply_narration_contract_hardening import patch_video


class NarrationContractDurationHardeningTests(unittest.TestCase):
    def test_measured_duration_replans_keep_protected_reflection(self):
        source = Path("app/services/video_generator.py").read_text(encoding="utf-8")
        patched = patch_video(source)

        self.assertIn(
            'str(planning_meta.get("reflection_text") or "").strip(), current_closing',
            patched,
        )
        self.assertIn(
            'current_opening, new_body_text, str(planning_meta.get("reflection_text") or "").strip()',
            patched,
        )
        self.assertIn(
            'float(planning_meta.get("reflection_duration_est_sec") or 0.0)',
            patched,
        )
        self.assertEqual(patch_video(patched), patched)


if __name__ == "__main__":
    unittest.main()
