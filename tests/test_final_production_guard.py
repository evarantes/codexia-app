import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from app.services.final_production_guard import (
    ensure_narrated_closing,
    install_final_production_guard,
    prepare_ptbr_tts_text,
)
from app.services.video_cost_reporting import build_task_cost_summary


class FinalProductionGuardTests(unittest.TestCase):
    def test_tts_restores_natural_jesus_spelling(self):
        source = "Jêzus está presente. JEZUS salva. Jesus é o Senhor."
        result = prepare_ptbr_tts_text(source)
        self.assertEqual(result, "Jesus está presente. Jesus salva. Jesus é o Senhor.")
        self.assertNotIn("Jêzus", result)

    def test_closing_and_cta_become_narrated_scene(self):
        plan = {
            "closing_message": "Cristo continua presente mesmo quando você não percebe.",
            "scenes": [
                {"text": "A fé nos ajuda a enxergar além do medo.", "image_path": "/tmp/old.png"},
                {"text": "Confie no cuidado de Deus para o próximo passo.", "image_path": "/tmp/repeated.png"},
            ],
        }
        result = ensure_narrated_closing(plan)
        self.assertEqual(len(result["scenes"]), 3)
        closing = result["scenes"][-1]
        self.assertIn("Cristo continua presente", closing["text"])
        self.assertIn("inscreva-se", closing["text"].lower())
        self.assertIn("curta", closing["text"].lower())
        self.assertIn("compartilhe", closing["text"].lower())
        self.assertTrue(closing["codexia_narrated_closing"])
        self.assertNotIn("image_path", closing)
        self.assertEqual(result["reflection_text"], "")
        self.assertEqual(result["closing_message"], "")

    def test_existing_cta_is_not_duplicated(self):
        plan = {
            "scenes": [
                {"text": "A esperança permanece."},
                {"text": "Curta esta mensagem, inscreva-se no canal e compartilhe com alguém."},
            ]
        }
        result = ensure_narrated_closing(plan)
        joined = " ".join(scene["text"] for scene in result["scenes"])
        self.assertEqual(joined.lower().count("inscreva-se"), 1)

    def test_compose_removes_silent_reflection(self):
        seen = {}

        class Dummy:
            def create_video(self, plan, *args, **kwargs):
                return plan

            def compose_video(self, *args, **kwargs):
                seen.update(kwargs)
                return kwargs

        install_final_production_guard(Dummy)
        instance = Dummy()
        instance.compose_video(
            reflection_text="texto mudo",
            closing_message="outro texto mudo",
            cta_text="cta mudo",
            ordinary="preservar",
        )
        self.assertIsNone(seen["reflection_text"])
        self.assertIsNone(seen["closing_message"])
        self.assertIsNone(seen["cta_text"])
        self.assertEqual(seen["ordinary"], "preservar")

    def test_visual_duplicate_causes_at_most_one_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            same1 = os.path.join(tmp, "same1.png")
            same2 = os.path.join(tmp, "same2.png")
            different = os.path.join(tmp, "different.png")
            Image.new("RGB", (96, 64), (120, 80, 40)).save(same1)
            Image.new("RGB", (96, 64), (120, 80, 40)).save(same2)
            img = Image.new("RGB", (96, 64), (20, 40, 120))
            for x in range(48, 96):
                for y in range(64):
                    img.putpixel((x, y), (230, 230, 230))
            img.save(different)

            class Dummy:
                def __init__(self):
                    self.paths = [same1, same2, different]
                    self.prompts = []

                def create_video(self, plan, *args, **kwargs):
                    return plan

                def compose_video(self, *args, **kwargs):
                    return kwargs

                def _ensure_image_for_scene(self, prompt, *args, **kwargs):
                    self.prompts.append(prompt)
                    return self.paths.pop(0)

            install_final_production_guard(Dummy)
            instance = Dummy()
            first = instance._ensure_image_for_scene("scene one")
            second = instance._ensure_image_for_scene("scene two")
            self.assertEqual(first, same1)
            self.assertEqual(second, different)
            self.assertEqual(len(instance.prompts), 3)
            self.assertIn("VISUAL UNIQUENESS", instance.prompts[-1])

    @mock.patch("app.services.video_cost_reporting._query_operations")
    def test_cost_summary_exposes_tracked_openai_cost_and_10min_projection(self, query_ops):
        query_ops.return_value = [
            {
                "capability": "IMAGE_GENERATION",
                "provider": "openai",
                "model": "gpt-image-2",
                "status": "completed",
                "estimated_cost_usd": 0.05,
                "actual_cost_usd": 0.0,
                "latency_ms": 100,
            },
            {
                "capability": "IMAGE_GENERATION",
                "provider": "openai",
                "model": "gpt-image-2",
                "status": "completed",
                "estimated_cost_usd": 0.05,
                "actual_cost_usd": 0.0,
                "latency_ms": 100,
            },
        ]
        summary = build_task_cost_summary("abc", {"result": {"payload": {"duration": 2}}})
        self.assertEqual(summary["model"], "gpt-image-2")
        self.assertEqual(summary["image_operation_count"], 2)
        self.assertGreater(summary["tracked_total_brl"], 0)
        self.assertGreater(summary["projected_10_min_brl"], summary["tracked_total_brl"])
        self.assertFalse(summary["is_official_invoice"])


if __name__ == "__main__":
    unittest.main()
