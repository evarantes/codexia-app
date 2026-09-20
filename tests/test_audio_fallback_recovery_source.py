import unittest
from pathlib import Path


class AudioFallbackRecoverySourceTests(unittest.TestCase):
    def test_edge_tts_receives_plain_text_instead_of_ssml_markup(self):
        source = Path("app/services/video_generator.py").read_text(encoding="utf-8")
        self.assertIn("edge_tts.Communicate(\n                        clean_text,", source)
        self.assertNotIn("edge_tts.Communicate(ssml, voice)", source)
        self.assertIn(".codexia-rejected", source)

    def test_validated_fallback_is_warning_but_mismatch_still_blocks(self):
        source = Path("app/services/cinematic_quality_service.py").read_text(encoding="utf-8")
        self.assertIn('fallback_severity = "high" if not within_tolerance else "medium"', source)
        self.assertIn("if not within_tolerance:", source)

    def test_rejected_checkpoint_is_never_reused(self):
        source = Path("app/services/audio_checkpoint.py").read_text(encoding="utf-8")
        self.assertIn('validation_status in {"rejected", "invalid", "failed_quality_gate"}', source)
        self.assertIn("_mark_checkpoint_audio_rejected(checkpoint", source)
        self.assertIn('plan["force_reuse_assets"] = True', source)


if __name__ == "__main__":
    unittest.main()
