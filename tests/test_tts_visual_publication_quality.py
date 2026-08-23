from __future__ import annotations

import math
import unittest

from app.services.narration_contract_guard import (
    NarrationContractError,
    install_narration_contract_guard,
    sanitize_narration_text,
    structural_issues,
    validate_narration_text,
)


class NarrationTechnicalMarkupRegressionTests(unittest.TestCase):
    def test_ssml_break_is_never_spoken(self):
        raw = 'Deus continua cuidando de você <break time="1200ms"/> mesmo no silêncio.'
        clean = sanitize_narration_text(raw)
        self.assertNotIn("break", clean.lower())
        self.assertNotIn("1200", clean)
        self.assertEqual(clean, "Deus continua cuidando de você, mesmo no silêncio.")
        self.assertEqual(validate_narration_text(raw), clean)

    def test_plain_text_pause_directives_are_removed(self):
        variants = (
            'Deus é fiel break time = "1.5s" continue confiando.',
            'Deus é fiel [pause duration=900ms] continue confiando.',
            'Deus é fiel (pausa duração=2s) continue confiando.',
        )
        for raw in variants:
            with self.subTest(raw=raw):
                clean = validate_narration_text(raw)
                folded = clean.lower()
                self.assertNotIn("break", folded)
                self.assertNotIn("pause", folded)
                self.assertNotIn("duração=", folded)
                self.assertTrue(clean.endswith("."))

    def test_safe_ssml_container_keeps_only_spoken_words(self):
        raw = '<speak><prosody rate="95%">A promessa permanece viva.</prosody></speak>'
        self.assertEqual(validate_narration_text(raw), "A promessa permanece viva.")

    def test_arbitrary_technical_fields_still_fail_closed(self):
        bad_samples = (
            "A mensagem continua. scene_id=4",
            "A mensagem continua. timestamp: 00:01:20",
            '{"narration_text": "A mensagem continua."}',
            "A mensagem continua. {{scene_duration}}",
        )
        for raw in bad_samples:
            with self.subTest(raw=raw):
                with self.assertRaises(NarrationContractError):
                    validate_narration_text(raw)

    def test_plain_portuguese_is_not_rewritten(self):
        raw = "Quando tudo parece silencioso, Deus continua trabalhando em seu favor."
        self.assertEqual(sanitize_narration_text(raw), raw)
        self.assertEqual(structural_issues(raw), [])

    def test_last_tts_boundary_receives_only_clean_text(self):
        class DummyGenerator:
            def __init__(self):
                self.received = None
                self._last_tts_debug = {}

            def generate_audio(self, text, *args, **kwargs):
                self.received = text
                return "fake.mp3"

        install_narration_contract_guard(DummyGenerator)
        self.assertGreaterEqual(
            int(getattr(DummyGenerator, "_codexia_narration_contract_guard_version", 0) or 0),
            2,
        )
        dummy = DummyGenerator()
        dummy.generate_audio('Escute com atenção <break time="1s"/> e guarde esta palavra.')
        self.assertEqual(dummy.received, "Escute com atenção, e guarde esta palavra.")
        self.assertNotIn("break", dummy.received.lower())
        self.assertTrue(dummy._last_tts_debug.get("tts_plain_text_only"))
        self.assertTrue(dummy._last_tts_debug.get("technical_markup_sanitized"))


class FinalPublicationQualityRegressionTests(unittest.TestCase):
    @staticmethod
    def _scene_visuals(count: int, unique: int):
        unique = max(1, unique)
        return [
            {"image_path": f"/data/media/images/scene_{idx % unique:02d}.png"}
            for idx in range(count)
        ]

    @staticmethod
    def _result(*, duration: float = 556.0, scenes: int = 48, unique_images: int = 40, tts_text: str | None = None):
        spoken = tts_text or "O Pastor conhece cada passo. Deus permanece perto em todos os momentos."
        return {
            "file_path": "/data/media/videos/final.mp4",
            "video_url": "/videos/final.mp4",
            "render_report": {
                "narration_plan": {
                    "opening_text": "O Pastor Que Conhece Cada Passo Seu.",
                    "closing_text": "",
                    "full_text": spoken,
                },
                "text_integrity": {
                    "final_text_sent_to_tts": spoken,
                    "captions_source_text": spoken,
                    "captions_match_narration_source": True,
                },
                "visual_plan": {
                    "generated_image_count": unique_images,
                    "reused_image_count": 0,
                    "average_image_duration_sec": duration / max(1, unique_images),
                },
                "scene_visuals": FinalPublicationQualityRegressionTests._scene_visuals(scenes, unique_images),
                "duration_plan": {
                    "obtained_duration_sec": duration,
                    "actual_audio_duration_sec": duration,
                },
            },
        }

    def _quality_gate(self, result, plan=None):
        # Import tardio: no CI o hardening pós-render é aplicado antes da suíte.
        from app.services.channel_excellence_guard import _quality_gate
        return _quality_gate(result, plan=plan or {})

    def test_ten_images_for_ten_minutes_is_review_only(self):
        result = self._result(unique_images=10)
        plan = {"scenes": [{} for _ in range(48)]}
        quality = self._quality_gate(result, plan)
        expected = min(48, math.ceil(556.0 / 15.0))
        self.assertEqual(quality["visual_density"]["required_unique_image_count"], expected)
        self.assertEqual(quality["visual_density"]["actual_unique_image_count"], 10)
        self.assertEqual(quality["visual_density"]["visual_density_deficit"], expected - 10)
        self.assertIn("visual_density_below_quality_target", quality["warnings"])
        self.assertTrue(quality["passed"])
        self.assertTrue(quality["review_recommended"])
        self.assertFalse(quality["publication_ready"])
        self.assertTrue(quality["auto_render_preserved"])

    def test_auto_upload_is_blocked_when_density_is_too_low(self):
        result = self._result(unique_images=10)
        plan = {
            "scenes": [{} for _ in range(48)],
            "_codexia_auto_upload_requested": True,
        }
        quality = self._quality_gate(result, plan)
        self.assertFalse(quality["passed"])
        self.assertFalse(quality["publication_ready"])
        self.assertIn("visual_density_below_publication_target", quality["blocking_violations"])
        self.assertIn("visual_density_below_quality_target", quality["warnings"])

    def test_dense_ten_minute_video_is_publication_ready(self):
        result = self._result(unique_images=40)
        plan = {"scenes": [{} for _ in range(48)]}
        quality = self._quality_gate(result, plan)
        self.assertTrue(quality["checks"]["visual_density_ok"])
        self.assertTrue(quality["checks"]["tts_plain_text_only"])
        self.assertTrue(quality["passed"])
        self.assertFalse(quality["review_recommended"])
        self.assertTrue(quality["publication_ready"])

    def test_old_render_with_break_markup_is_not_publishable(self):
        spoken = 'Deus continua cuidando de você <break time="1s"/> mesmo quando você não percebe.'
        result = self._result(unique_images=40, tts_text=spoken)
        plan = {"scenes": [{} for _ in range(48)]}
        quality = self._quality_gate(result, plan)
        self.assertFalse(quality["checks"]["tts_plain_text_only"])
        self.assertFalse(quality["passed"])
        self.assertFalse(quality["publication_ready"])
        self.assertIn("tts_text_contains_technical_markup", quality["blocking_violations"])


if __name__ == "__main__":
    unittest.main()
