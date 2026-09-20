import unittest
from pathlib import Path


class AudioFallbackRecoverySourceTests(unittest.TestCase):
    def test_edge_tts_uses_chunked_plain_text_performance_with_exact_timing(self):
        source = Path("app/services/video_generator.py").read_text(encoding="utf-8")
        self.assertIn("synthesize_edge_ptbr_performance(", source)
        self.assertIn("spoken_text=clean_text", source)
        self.assertIn('tts_debug["caption_alignment_exact"]', source)
        self.assertIn('"caption_timing_source": "edge_tts_exact_ptbr_fallback"', source)
        self.assertNotIn("edge_tts.Communicate(ssml, voice)", source)
        self.assertIn(".codexia-rejected", source)

    def test_validated_fallback_is_warning_but_mismatch_still_blocks(self):
        source = Path("app/services/cinematic_quality_service.py").read_text(encoding="utf-8")
        self.assertIn('fallback_severity = "high" if not within_tolerance else "medium"', source)
        self.assertIn("if not within_tolerance:", source)

    def test_exact_provider_boundaries_can_replace_unavailable_asr_evidence(self):
        source = Path("app/services/cinematic_quality_service.py").read_text(encoding="utf-8")
        proof = source.index("provider_alignment_exact = bool(")
        override = source.index("if not within_tolerance and provider_alignment_exact:")
        rejection = source.index("if not within_tolerance:", override)

        self.assertLess(proof, override)
        self.assertLess(override, rejection)
        self.assertIn('startswith("edge_tts_exact_ptbr")', source)
        self.assertIn("similarity = 1.0", source[override:rejection])

    def test_unproven_fallback_mismatch_remains_blocking(self):
        source = Path("app/services/cinematic_quality_service.py").read_text(encoding="utf-8")
        self.assertIn('severity="high"', source)
        self.assertIn("approved = not blocking_issues", source)

    def test_rejected_checkpoint_is_never_reused(self):
        source = Path("app/services/audio_checkpoint.py").read_text(encoding="utf-8")
        self.assertIn('validation_status in {"rejected", "invalid", "failed_quality_gate"}', source)
        self.assertIn("_mark_checkpoint_audio_rejected(checkpoint", source)
        self.assertIn('plan["force_reuse_assets"] = True', source)


if __name__ == "__main__":
    unittest.main()
