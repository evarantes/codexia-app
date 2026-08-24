from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARDENING = ROOT / "scripts" / "apply_lightweight_stage6_recovery.py"
CHAIN = ROOT / "scripts" / "apply_ready_video_asset_repair_v3.py"
RENDERER = ROOT / "app" / "services" / "lightweight_recovery_renderer.py"
OPTIMIZER = ROOT / "app" / "services" / "intelligent_cost_optimizer.py"


class LightweightStage6RecoveryHardeningTests(unittest.TestCase):
    def test_hardening_explains_visual_tradeoff_before_execution(self):
        text = HARDENING.read_text(encoding="utf-8")
        required = (
            "CODEXIA_LIGHTWEIGHT_STAGE6_RECOVERY_V1",
            "Renderização de recuperação: FFmpeg local leve",
            "zoom, pan e Ken Burns calculados quadro a quadro serão simplificados",
            "Legendas: serão preservadas no tempo do áudio.",
            "Chamadas externas de música: 0.",
            'payload["lightweight_recovery_render_confirmed"] = True',
        )
        for token in required:
            self.assertIn(token, text)

    def test_fast_path_runs_only_after_confirmed_hash_payload(self):
        text = HARDENING.read_text(encoding="utf-8")
        self.assertIn('plan.get("force_render_only")', text)
        self.assertIn('plan.get("lightweight_recovery_render_confirmed")', text)
        self.assertIn("render_lightweight_recovery_video", text)
        self.assertIn('"paid_image_calls": 0', text)
        self.assertIn('"paid_tts_calls": 0', text)
        self.assertIn('"external_music_provider_calls": 0', text)
        self.assertIn('source="lightweight_recovery_renderer"', text)

    def test_ready_repair_chain_applies_lightweight_after_intelligent_cost(self):
        text = CHAIN.read_text(encoding="utf-8")
        self.assertIn("apply_lightweight_stage6_recovery as lightweight_stage6", text)
        self.assertLess(text.index("intelligent_cost.apply()"), text.index("lightweight_stage6.apply()"))
        self.assertLess(text.index("intelligent_cost.check()"), text.index("lightweight_stage6.check()"))

    def test_renderer_has_no_download_or_ai_provider_calls(self):
        text = RENDERER.read_text(encoding="utf-8")
        forbidden = (
            "requests.get(",
            "generate_music(",
            "generate_image(",
            "huggingface",
            "http://",
            "https://",
        )
        for token in forbidden:
            self.assertNotIn(token, text.lower())
        self.assertIn('"paid_provider_calls": 0', text)
        self.assertIn('"external_downloads": 0', text)
        self.assertIn("-threads", text)

    def test_optimizer_hash_binds_renderer_change_and_caption_preservation(self):
        text = OPTIMIZER.read_text(encoding="utf-8")
        self.assertIn('"render_strategy": "ffmpeg_lightweight_recovery_v1" if lightweight else "original_renderer"', text)
        self.assertIn('"lightweight_recovery": lightweight', text)
        self.assertIn('"preserve_captions": True', text)
        self.assertIn('"never_remove_captions": True', text)
        self.assertIn('"never_regenerate_paid_tts": True', text)


if __name__ == "__main__":
    unittest.main()
