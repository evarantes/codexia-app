from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.services.fabrica_pipeline import (
    apply_fabrica_scene_contract,
    camera_effect_from_direction,
    character_bible_prompt,
    generate_eleven_music_track,
    install_fabrica_pipeline_patch,
    split_visual_directions,
)


class FabricaPipelineTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "ENABLE_FABRICA_PIPELINE",
            "ENABLE_FABRICA_CHARACTER_BIBLE",
            "FABRICA_MUSIC_PROVIDER",
            "ELEVENLABS_API_KEY",
        ):
            os.environ.pop(key, None)

    def test_camera_direction_is_removed_from_spoken_text(self):
        spoken, direction = split_visual_directions(
            'Maria respira fundo. A câmera foca em Maria, no lado direito, enquanto ela segura a carta.'
        )

        self.assertEqual(spoken, "Maria respira fundo.")
        self.assertIn("A câmera foca em Maria", direction)
        effect = camera_effect_from_direction(direction)
        self.assertEqual(effect["type"], "focus")
        self.assertEqual(effect["side"], "right")
        self.assertEqual(effect["target"], "Maria")

    def test_contract_preserves_source_and_maps_visual_motion(self):
        plan = {
            "title": "A carta",
            "character_bible": [{"name": "Maria", "fixed_visual_description": "mulher adulta, vestido azul"}],
            "scenes": [{
                "text": 'Maria lê a carta. A câmera foca em Maria, no lado esquerdo.',
                "image_prompt": "Maria reading a letter in a quiet room",
            }],
        }

        directed, report = apply_fabrica_scene_contract(plan)
        scene = directed["scenes"][0]

        self.assertEqual(scene["text"], "Maria lê a carta.")
        self.assertIn("A câmera foca em Maria", scene["visual_direction"])
        self.assertEqual(scene["motion_effect"], "push_in")
        self.assertIn("nunca narrar", scene["image_prompt"])
        self.assertEqual(scene["_fabrica_original_text"], plan["scenes"][0]["text"])
        self.assertEqual(report["directions_separated"], 1)
        self.assertEqual(report["character_bible"]["source"], "plan_character_bible")
        self.assertTrue(directed["fabrica_pipeline"]["directions_removed_from_speech"])

    def test_missing_character_bible_gets_automatic_identity_lock(self):
        bible = character_bible_prompt(
            {"title": "Maria e Jesus"},
            [{"text": "Maria encontra Jesus ao amanhecer."}],
        )

        self.assertEqual(bible["source"], "automatic_identity_lock")
        self.assertIn("Maria", bible["characters"])
        self.assertIn("Jesus", bible["characters"])
        self.assertIn("primeira aparição", bible["prompt"])

    def test_patch_adds_bible_to_every_image_prompt(self):
        class DummyGenerator:
            def _ensure_image_for_scene(self, prompt, *args, **kwargs):
                return prompt

            def create_video_from_plan(self, plan, *args, **kwargs):
                return {"render_report": {}, "image_prompt": self._ensure_image_for_scene("Maria caminha")}

        install_fabrica_pipeline_patch(DummyGenerator)
        generator = DummyGenerator()
        plan = {"scenes": [{"text": "Maria caminha."}]}
        result = generator.create_video_from_plan(plan)
        prompt = result["image_prompt"]

        self.assertIn("BÍBLIA VISUAL FIXA", prompt)
        self.assertIn("Maria", prompt)
        self.assertEqual(result["fabrica_pipeline"]["version"], "fabrica-structure-v1")

    def test_eleven_music_is_optional_and_fail_open_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_eleven_music_track(
                {"title": "Teste", "scenes": [{"text": "Esperança."}]},
                tmp,
                60,
            )
        self.assertIsNone(result)

    def test_eleven_music_response_is_cached(self):
        class Response:
            ok = True
            content = b"mp3" * 1000

        os.environ["ELEVENLABS_API_KEY"] = "test-key"
        os.environ["FABRICA_MUSIC_PROVIDER"] = "elevenlabs"
        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.services.fabrica_pipeline.requests.post", return_value=Response()
        ) as post:
            first = generate_eleven_music_track(
                {"title": "Teste", "scenes": [{"text": "Esperança."}]},
                tmp,
                60,
            )
            second = generate_eleven_music_track(
                {"title": "Teste", "scenes": [{"text": "Esperança."}]},
                tmp,
                60,
            )

        self.assertIsNotNone(first)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
