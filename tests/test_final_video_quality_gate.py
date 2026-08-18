from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.channel_excellence_guard import install_channel_excellence_guard_patch
from app.services.final_video_presentation_guard import install_final_video_presentation_guard


class QualityGenerator:
    def __init__(self, tmp_path: Path | None = None):
        self.tmp_path = tmp_path
        self.audio_text = ""
        self.endcard_aspect_ratio = None

    def generate_audio(self, text, lang="pt", *args, **kwargs):
        self.audio_text = text
        return {"text": text, "lang": lang}

    def _default_opening_text(self, channel_name, *, plan=None):
        return "Prepare o coração: há uma mensagem de fé para o seu dia."

    def _default_reflection_text(self, plan=None, scenes=None):
        return "Reflexão automática escondida."

    def _default_closing_text(self, channel_name):
        return "Continue conosco e acompanhe as próximas mensagens de fé."

    def _target_visual_count(self, scenes, plan=None, *, ai_available=True):
        return 1

    def _build_visual_transition_decision(self, previous_profile, current_profile):
        return {"should_generate_new": False, "reason": "reuse"}

    def _resolve_contextual_closing(self, plan=None):
        return {"kind": "custom", "lines": ["Leve esta esperança com você."]}

    def create_video_from_plan(self, plan, *args, **kwargs):
        return {
            "file_path": "/tmp/fake.mp4",
            "render_report": {
                "narration_plan": {
                    "opening_text": str(plan.get("title") or ""),
                    "closing_text": "",
                },
                "visual_plan": {
                    "generated_image_count": 8,
                    "reused_image_count": 0,
                    "average_image_duration_sec": 7.5,
                },
            },
        }

    def _resolve_closing_background_image(self, branding, **kwargs):
        return {"path": None, "source": "dedicated_generated_endcard"}

    def _ensure_image_for_scene(self, prompt, text_fallback, aspect_ratio="16:9", **kwargs):
        if "no people" not in prompt.lower():
            raise AssertionError("Endcard premium deve ser gerado sem pessoas.")
        self.endcard_aspect_ratio = aspect_ratio
        path = (self.tmp_path or Path("/tmp")) / "premium-endcard.png"
        path.write_bytes(b"fake-image")
        return str(path)


def _fresh_cls(name: str):
    return type(name, (QualityGenerator,), {})


class FinalVideoQualityGateTests(unittest.TestCase):
    _ENV_KEYS = (
        "ENABLE_APPROVED_NARRATION_ONLY",
        "ENABLE_CHANNEL_EXCELLENCE_GUARD",
        "ENABLE_STRICT_VISUAL_UNIQUENESS",
        "ENABLE_FINAL_VIDEO_QUALITY_GATE",
        "ENABLE_AI_PREMIUM_ENDCARD",
    )

    def tearDown(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    def test_approved_narration_only_removes_generic_intro_reflection_and_spoken_cta(self):
        os.environ["ENABLE_APPROVED_NARRATION_ONLY"] = "true"
        cls = _fresh_cls("ApprovedNarrationGenerator")
        install_channel_excellence_guard_patch(cls)
        instance = cls()

        plan = {"title": "Jesus Está no Centro da Sua Vida?"}
        opening = instance._default_opening_text("Canal", plan=plan)
        self.assertEqual(opening, plan["title"])
        self.assertNotIn("mensagem de fé", opening.lower())
        self.assertEqual(instance._default_reflection_text(plan, []), "")
        self.assertEqual(instance._default_closing_text("Canal"), "")

    def test_ptbr_guard_normalizes_jesus_and_pelo_contrario(self):
        os.environ["ENABLE_CHANNEL_EXCELLENCE_GUARD"] = "true"
        cls = _fresh_cls("PtBrFinalGenerator")
        install_channel_excellence_guard_patch(cls)
        result = cls().generate_audio("Jesus disse: pelo contrário, continue.", lang="pt-BR")
        self.assertEqual(result["lang"], "pt")
        self.assertIn("Jêzus", result["text"])
        self.assertIn("muito pelo contrário", result["text"].lower())

    def test_auto_video_requires_one_visual_target_per_scene(self):
        os.environ["ENABLE_STRICT_VISUAL_UNIQUENESS"] = "true"
        cls = _fresh_cls("UniqueVisualGenerator")
        install_channel_excellence_guard_patch(cls)
        instance = cls()
        scenes = [{"text": f"Cena {idx}"} for idx in range(9)]
        self.assertEqual(instance._target_visual_count(scenes, {}), 9)
        decision = instance._build_visual_transition_decision({}, {})
        self.assertTrue(decision["should_generate_new"])
        self.assertTrue(decision["forced_by_channel_excellence"])

    def test_manual_single_image_mode_is_preserved(self):
        os.environ["ENABLE_STRICT_VISUAL_UNIQUENESS"] = "true"
        cls = _fresh_cls("ManualSingleVisualGenerator")
        install_channel_excellence_guard_patch(cls)
        scenes = [{"text": "A"}, {"text": "B"}]
        self.assertEqual(cls()._target_visual_count(scenes, {"selected_images": ["/tmp/manual.png"]}), 1)

    def test_manual_visuals_do_not_trigger_paid_uniqueness_rejection(self):
        os.environ["ENABLE_FINAL_VIDEO_QUALITY_GATE"] = "true"

        class ManualGenerator(QualityGenerator):
            def create_video_from_plan(self, plan, *args, **kwargs):
                return {
                    "file_path": "/tmp/fake.mp4",
                    "render_report": {
                        "narration_plan": {"opening_text": plan.get("title", ""), "closing_text": ""},
                        "visual_plan": {
                            "generated_image_count": 3,
                            "reused_image_count": 2,
                            "average_image_duration_sec": 18.0,
                        },
                    },
                }

        cls = type("ManualQualityGenerator", (ManualGenerator,), {})
        install_channel_excellence_guard_patch(cls)
        result = cls().create_video_from_plan({
            "title": "Teste",
            "selected_images": ["/tmp/a.png", "/tmp/b.png"],
            "scenes": [{"text": "A"}, {"text": "B"}],
        })
        quality = result["channel_excellence_guard"]["quality_gate"]
        self.assertTrue(quality["manual_visuals"])
        self.assertTrue(quality["passed"])

    def test_quality_gate_blocks_generic_opening_and_reused_generated_paths(self):
        os.environ["ENABLE_FINAL_VIDEO_QUALITY_GATE"] = "true"

        class BadGenerator(QualityGenerator):
            def create_video_from_plan(self, plan, *args, **kwargs):
                return {
                    "file_path": "/tmp/fake.mp4",
                    "render_report": {
                        "narration_plan": {
                            "opening_text": "Uma mensagem de fé para hoje.",
                            "closing_text": "",
                        },
                        "visual_plan": {
                            "generated_image_count": 8,
                            "reused_image_count": 2,
                            "average_image_duration_sec": 14.0,
                        },
                    },
                }

        cls = type("BlockedQualityGenerator", (BadGenerator,), {})
        install_channel_excellence_guard_patch(cls)
        with self.assertRaisesRegex(RuntimeError, "controle final de qualidade"):
            cls().create_video_from_plan({"title": "Teste", "scenes": [{"text": "Mensagem"}]})

    def test_premium_endcard_generates_dedicated_ai_background(self):
        os.environ["ENABLE_AI_PREMIUM_ENDCARD"] = "true"
        cls = _fresh_cls("PremiumEndcardGenerator")
        install_final_video_presentation_guard(cls)
        with tempfile.TemporaryDirectory() as tmp:
            instance = cls(tmp_path=Path(tmp))
            result = instance._resolve_closing_background_image({})
            self.assertEqual(result["source"], "generated_premium_endcard_ai")
            self.assertEqual(result["aspect_ratio"], "16:9")
            self.assertTrue(Path(result["path"]).exists())

    def test_premium_endcard_preserves_vertical_aspect_ratio(self):
        os.environ["ENABLE_AI_PREMIUM_ENDCARD"] = "true"
        cls = _fresh_cls("VerticalPremiumEndcardGenerator")
        install_final_video_presentation_guard(cls)
        with tempfile.TemporaryDirectory() as tmp:
            instance = cls(tmp_path=Path(tmp))
            result = instance._resolve_closing_background_image({"aspect_ratio": "9:16"})
            self.assertEqual(result["source"], "generated_premium_endcard_ai")
            self.assertEqual(result["aspect_ratio"], "9:16")
            self.assertEqual(instance.endcard_aspect_ratio, "9:16")

    def test_quality_gate_accepts_clean_result(self):
        os.environ["ENABLE_FINAL_VIDEO_QUALITY_GATE"] = "true"
        cls = _fresh_cls("CleanQualityGenerator")
        install_channel_excellence_guard_patch(cls)
        result = cls().create_video_from_plan({"title": "Título aprovado", "scenes": [{"text": "Mensagem"}]})
        quality = result["channel_excellence_guard"]["quality_gate"]
        self.assertTrue(quality["passed"])
        self.assertEqual(quality["violations"], [])


if __name__ == "__main__":
    unittest.main()
