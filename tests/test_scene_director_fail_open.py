"""Regression tests for non-blocking scene director failures."""
import unittest
from unittest.mock import patch

from app.services.scene_director_active import install_scene_director_active_patch


class SceneDirectorFailOpenTests(unittest.TestCase):
    def test_director_error_renders_original_plan_and_reports_fallback(self):
        class Generator:
            def create_video_from_plan(self, plan):
                return {"received_plan": plan}

        original = {"scenes": [{"narration": "Texto completo.", "image_prompt": "Cena"}]}
        with patch("app.services.scene_director_active.direct_scene_plan", side_effect=ValueError("bad scene")):
            install_scene_director_active_patch(Generator)
            result = Generator().create_video_from_plan(original)
        self.assertIs(result["received_plan"], original)
        self.assertEqual(result["scene_director"]["mode"], "fallback")
        self.assertEqual(result["scene_director"]["error_type"], "ValueError")
        self.assertFalse(result["scene_director"]["blocking"])
        self.assertEqual(original["scenes"][0]["narration"], "Texto completo.")

    def test_render_failure_is_not_hidden(self):
        class Generator:
            def create_video_from_plan(self, plan):
                raise RuntimeError("render unavailable")

        with patch("app.services.scene_director_active.direct_scene_plan", side_effect=ValueError("bad scene")):
            install_scene_director_active_patch(Generator)
            with self.assertRaisesRegex(RuntimeError, "render unavailable"):
                Generator().create_video_from_plan({"scenes": []})


if __name__ == "__main__":
    unittest.main()
