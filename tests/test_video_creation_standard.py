import unittest

from app.services.narration_contract_guard import (
    has_complete_cta,
    install_narration_contract_guard,
)
from app.services.video_creation_standard import (
    STANDARD_COMPLETE_CTA,
    STANDARD_REQUIRED_CTA_SIGNALS,
    VIDEO_CREATION_STANDARD_VERSION,
    apply_standard_video_structure,
    should_apply_video_creation_standard,
)


class VideoCreationStandardTests(unittest.TestCase):
    def test_default_narrated_plan_receives_canonical_structure(self):
        plan = {"title": "Jesus, o motivo de eu existir", "kind": "devotional"}

        returned = apply_standard_video_structure(plan)

        self.assertIs(returned, plan)
        self.assertEqual(plan["codexia_video_standard_version"], VIDEO_CREATION_STANDARD_VERSION)
        self.assertEqual(plan["bg_music_volume"], 0.025)
        self.assertEqual(plan["music_mood"], "peaceful")
        self.assertTrue(plan["captions_enabled"])
        self.assertTrue(plan["automatic_opening"])
        self.assertTrue(plan["automatic_closing"])
        self.assertEqual(plan["caption_max_lines"], 2)
        self.assertEqual(plan["narrated_cta_text"], STANDARD_COMPLETE_CTA)
        self.assertIn("request_wins", plan["codexia_video_standard"]["duration_policy"])

    def test_explicit_render_choices_are_preserved(self):
        plan = {
            "title": "Vídeo personalizado",
            "bg_music_volume": 0.08,
            "music_mood": "custom",
            "music_prompt": "my own prompt",
            "caption_max_lines": 1,
            "narrated_cta_text": "CTA personalizado.",
            "target_duration_sec": 720,
        }

        apply_standard_video_structure(plan)

        self.assertEqual(plan["bg_music_volume"], 0.08)
        self.assertEqual(plan["music_mood"], "custom")
        self.assertEqual(plan["music_prompt"], "my own prompt")
        self.assertEqual(plan["caption_max_lines"], 1)
        self.assertEqual(plan["narrated_cta_text"], "CTA personalizado.")
        self.assertEqual(plan["target_duration_sec"], 720)

    def test_standard_does_not_force_five_minute_duration(self):
        plan = {"title": "Sem duração explícita"}
        apply_standard_video_structure(plan)
        self.assertNotIn("target_duration_sec", plan)
        self.assertNotIn("duration_minutes", plan)

    def test_special_modes_are_not_rewritten(self):
        for kind in ("music", "short", "youtube_shorts"):
            with self.subTest(kind=kind):
                plan = {"kind": kind, "title": "Especial"}
                self.assertFalse(should_apply_video_creation_standard(plan))
                apply_standard_video_structure(plan)
                self.assertNotIn("narrated_cta_text", plan)
                self.assertNotIn("codexia_video_standard_version", plan)

    def test_standard_can_be_explicitly_disabled(self):
        plan = {"title": "Especial", "disable_standard_video_structure": True}
        self.assertFalse(should_apply_video_creation_standard(plan))
        apply_standard_video_structure(plan)
        self.assertNotIn("codexia_video_standard_version", plan)

    def test_cta_contract_requires_like_subscribe_bell_share(self):
        self.assertEqual(
            STANDARD_REQUIRED_CTA_SIGNALS,
            frozenset({"like", "subscribe", "bell", "share"}),
        )
        folded = STANDARD_COMPLETE_CTA.lower()
        self.assertIn("curta", folded)
        self.assertIn("inscreva", folded)
        self.assertIn("sininho", folded)
        self.assertIn("compartilh", folded)


class _DummyVideoGenerator:
    def _count_words(self, text):
        return len(str(text or "").split())

    def _estimate_text_duration_with_voice(self, text, **_kwargs):
        return len(str(text or "").split()) / 2.4

    def prepare_final_narration_text(self, plan, scenes, voice_style=None, voice_gender=None):
        return {
            "opening_text": "Hoje existe uma mensagem importante para você.",
            "body_text": "Jesus continua sendo nossa esperança.",
            "reflection_text": "Leve esta verdade para o seu coração.",
            "cta_text": "Inscreva-se no canal.",
            "closing_text": "Inscreva-se no canal.",
            "intro_opening_hold_sec": 0.4,
            "opening_duration_est_sec": 2.0,
            "body_duration_est_sec": 4.0,
            "pause_duration_sec": 0.5,
        }

    def generate_audio(self, text, *args, **kwargs):
        return "/tmp/nonexistent.mp3"

    def _compose_segmented_narration_audio(self, *, main_text, cta_text, **kwargs):
        return {"main_text": main_text, "cta_text": cta_text}


class VideoCreationStandardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_narration_contract_guard(_DummyVideoGenerator)

    def test_guard_applies_standard_to_runtime_plan_and_repairs_incomplete_cta(self):
        generator = _DummyVideoGenerator()
        plan = {"title": "Jesus, o motivo de eu existir", "kind": "devotional"}
        scenes = [{"text": "Jesus nos chama para perto."}]

        meta = generator.prepare_final_narration_text(plan, scenes)

        self.assertEqual(plan["music_mood"], "peaceful")
        self.assertEqual(plan["bg_music_volume"], 0.025)
        self.assertTrue(plan["captions_enabled"])
        self.assertTrue(has_complete_cta(meta["cta_text"]))
        self.assertIn("curta", meta["cta_text"].lower())
        self.assertEqual(meta["protected_closing_contract"]["version"], 3)
        self.assertEqual(
            set(meta["protected_closing_contract"]["required_cta_signals"]),
            set(STANDARD_REQUIRED_CTA_SIGNALS),
        )


if __name__ == "__main__":
    unittest.main()
