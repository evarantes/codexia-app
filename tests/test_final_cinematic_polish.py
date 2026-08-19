from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.final_cinematic_polish import install_final_cinematic_polish


class _DummyGenerator:
    def __init__(self):
        self.prompts = []

    def _split_caption_units(self, text, max_words=8, max_chars=54):
        return [str(text)]

    def _ensure_image_for_scene(self, prompt, *args, **kwargs):
        self.prompts.append(str(prompt))
        return "/tmp/fake-scene.png"

    def _resolve_closing_background_image(self, branding, *args, **kwargs):
        return {"path": None, "source": "last_scene", "aspect_ratio": kwargs.get("aspect_ratio", "16:9")}


class FinalCinematicPolishTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "ENABLE_SEMANTIC_CAPTION_CHUNKS",
            "ENABLE_STRONG_VISUAL_DIVERSITY",
            "ENABLE_GUARANTEED_DISTINCT_ENDCARD",
        ):
            os.environ.pop(key, None)

    def test_caption_units_do_not_leave_single_word_fragments_when_merge_is_possible(self):
        cls = type("CaptionGenerator", (_DummyGenerator,), {})
        install_final_cinematic_polish(cls)
        generator = cls()
        units = generator._split_caption_units(
            "A presença de Cristo transforma nossas escolhas, mesmo quando o caminho parece difícil."
        )
        self.assertGreaterEqual(len(units), 2)
        self.assertTrue(all(len(unit.split()) >= 2 for unit in units))
        self.assertTrue(all(len(unit) <= 48 for unit in units))

    def test_visual_generation_rotates_strongly_different_directives(self):
        cls = type("VisualGenerator", (_DummyGenerator,), {})
        install_final_cinematic_polish(cls)
        generator = cls()
        generator._ensure_image_for_scene("Jesus caminha com seus discípulos")
        generator._ensure_image_for_scene("Jesus conversa com uma família")
        generator._ensure_image_for_scene("Uma reflexão sobre esperança")
        self.assertEqual(len(generator.prompts), 3)
        self.assertIn("wide environmental establishing", generator.prompts[0])
        self.assertIn("visible physical action", generator.prompts[1])
        self.assertIn("symbolic cutaway", generator.prompts[2])
        self.assertNotEqual(generator.prompts[0], generator.prompts[1])

    def test_endcard_never_reuses_last_story_scene(self):
        cls = type("EndcardGenerator", (_DummyGenerator,), {})
        install_final_cinematic_polish(cls)
        generator = cls()
        resolved = generator._resolve_closing_background_image({}, aspect_ratio="16:9")
        self.assertNotEqual(resolved.get("source"), "last_scene")
        self.assertTrue(resolved.get("cinematic_polish"))
        if resolved.get("path"):
            self.assertTrue(Path(resolved["path"]).exists())

    def test_explicit_disable_preserves_original_caption_split(self):
        os.environ["ENABLE_SEMANTIC_CAPTION_CHUNKS"] = "false"
        cls = type("RollbackGenerator", (_DummyGenerator,), {})
        install_final_cinematic_polish(cls)
        generator = cls()
        self.assertEqual(generator._split_caption_units("texto simples"), ["texto simples"])


if __name__ == "__main__":
    unittest.main()
