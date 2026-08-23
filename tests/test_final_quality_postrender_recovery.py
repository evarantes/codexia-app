from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.services.channel_excellence_guard import (
    _quality_gate,
    install_channel_excellence_guard_patch,
)


def _result(*, closing: str, spoken: str, captions: str, opening: str = "O Pastor Que Conhece Cada Passo Seu.", file_path: str = "/tmp/final.mp4"):
    return {
        "file_path": file_path,
        "render_report": {
            "narration_plan": {
                "opening_text": opening,
                "closing_text": closing,
            },
            "text_integrity": {
                "final_text_sent_to_tts": spoken,
                "captions_source_text": captions,
                "captions_match_narration_source": spoken == captions,
            },
            "sync_validation": {
                "captions_synced_with_audio": spoken == captions,
            },
            "visual_plan": {
                "generated_image_count": 0,
                "reused_image_count": 0,
                "average_image_duration_sec": 0.0,
            },
        },
    }


class FinalQualityPostrenderRecoveryTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "ENABLE_CHANNEL_EXCELLENCE_GUARD",
            "ENABLE_FINAL_VIDEO_QUALITY_GATE",
            "ENABLE_DURATION_SANITY_PREFLIGHT",
        ):
            os.environ.pop(key, None)

    def test_real_spoken_and_captioned_closing_is_not_hidden(self):
        closing = "Inscreva-se no canal, ative o sininho e compartilhe esta mensagem."
        full = "O Pastor Que Conhece Cada Passo Seu. Deus conhece cada passo. " + closing
        quality = _quality_gate(
            _result(closing=closing, spoken=full, captions=full),
            plan={"title": "O Pastor Que Conhece Cada Passo Seu"},
        )

        self.assertTrue(quality["passed"])
        self.assertTrue(quality["checks"]["closing_present_in_canonical_audio"])
        self.assertTrue(quality["checks"]["closing_present_in_captions"])
        self.assertNotIn("hidden_spoken_closing", quality["blocking_violations"])
        self.assertNotIn("hidden_spoken_closing", quality["warnings"])

    def test_legacy_hidden_closing_after_completed_render_becomes_review_warning(self):
        closing = "Continue conosco e acompanhe as próximas mensagens de fé."
        spoken = "O Pastor Que Conhece Cada Passo Seu. Deus conhece cada passo da sua caminhada."
        quality = _quality_gate(
            _result(closing=closing, spoken=spoken, captions=spoken, file_path="/data/media/videos/final.mp4"),
            plan={"title": "O Pastor Que Conhece Cada Passo Seu"},
        )

        self.assertTrue(quality["passed"])
        self.assertEqual(quality["blocking_violations"], [])
        self.assertIn("hidden_spoken_closing", quality["editorial_warnings"])
        self.assertTrue(quality["review_recommended"])
        self.assertTrue(quality["auto_render_preserved"])
        self.assertEqual(quality["late_quality_policy"], "preserve_valid_render_review_first_v1")

    def test_hidden_closing_without_render_candidate_remains_blocking(self):
        closing = "Continue conosco e acompanhe as próximas mensagens de fé."
        spoken = "O Pastor Que Conhece Cada Passo Seu. Deus conhece cada passo."
        quality = _quality_gate(
            _result(closing=closing, spoken=spoken, captions=spoken, file_path=""),
            plan={"title": "O Pastor Que Conhece Cada Passo Seu"},
        )

        self.assertFalse(quality["passed"])
        self.assertIn("hidden_spoken_closing", quality["blocking_violations"])

    def test_generic_opening_stays_blocking_even_when_mp4_exists(self):
        quality = _quality_gate(
            _result(
                closing="",
                spoken="Uma mensagem de fé para hoje. Conteúdo.",
                captions="Uma mensagem de fé para hoje. Conteúdo.",
                opening="Uma mensagem de fé para hoje.",
                file_path="/data/media/videos/final.mp4",
            ),
            plan={"title": "Teste"},
        )

        self.assertFalse(quality["passed"])
        self.assertIn("generic_automatic_opening", quality["blocking_violations"])

    def test_recovery_image_budget_violation_stays_blocking_after_render(self):
        result = _result(
            closing="",
            spoken="O Pastor Que Conhece Cada Passo Seu. Conteúdo concluído.",
            captions="O Pastor Que Conhece Cada Passo Seu. Conteúdo concluído.",
            file_path="/data/media/videos/final.mp4",
        )
        result["render_report"]["visual_plan"]["recovery_image_budget"] = {
            "used_new_image_calls": 2,
            "allowed_new_image_calls": 1,
        }
        budget = {
            "enabled": True,
            "allowed_new_image_calls": 1,
            "confirmed_max_image_cost_usd": 1.0,
            "confirmed_max_image_cost_brl": 6.0,
        }

        with patch("app.services.channel_excellence_guard._recovery_visual_budget", return_value=budget):
            quality = _quality_gate(result, plan={"title": "Teste"})

        self.assertFalse(quality["passed"])
        self.assertIn("confirmed_recovery_image_budget_exceeded", quality["blocking_violations"])

    def test_wrapper_does_not_throw_after_valid_mp4_for_legacy_hidden_closing(self):
        os.environ["ENABLE_FINAL_VIDEO_QUALITY_GATE"] = "true"
        os.environ["ENABLE_DURATION_SANITY_PREFLIGHT"] = "false"

        class LegacyCompletedGenerator:
            def _resolve_contextual_closing(self, plan=None):
                return {"lines": ["Leve esta esperança com você."]}

            def create_video_from_plan(self, plan, *args, **kwargs):
                closing = "Continue conosco e acompanhe as próximas mensagens de fé."
                spoken = "O Pastor Que Conhece Cada Passo Seu. Deus conhece cada passo."
                return _result(
                    closing=closing,
                    spoken=spoken,
                    captions=spoken,
                    file_path="/data/media/videos/final.mp4",
                )

        cls = type("LegacyCompletedQualityGenerator", (LegacyCompletedGenerator,), {})
        install_channel_excellence_guard_patch(cls)
        result = cls().create_video_from_plan({
            "title": "O Pastor Que Conhece Cada Passo Seu",
            "scenes": [{"text": "Deus conhece cada passo."}],
        })

        quality = result["channel_excellence_guard"]["quality_gate"]
        self.assertTrue(quality["passed"])
        self.assertIn("hidden_spoken_closing", quality["editorial_warnings"])
        self.assertEqual(quality["blocking_violations"], [])


if __name__ == "__main__":
    unittest.main()
