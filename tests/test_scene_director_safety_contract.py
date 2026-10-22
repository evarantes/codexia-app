"""Auditoria preventiva do contrato do diretor de cenas.

Testes deliberadamente separados do PR #267: nao alteram producao.
Executar depois de integrar o tratamento de fallback em ambiente de teste.
"""
import unittest
from unittest.mock import patch

from app.services.scene_director_active import install_scene_director_active_patch


class SceneDirectorSafetyContractTests(unittest.TestCase):
    def test_director_failure_keeps_original_plan_without_mutation(self):
        class Generator:
            def create_video_from_plan(self, plan):
                return {"received_plan": plan}

        original = {"scenes": [{"narration": "Narracao integral.", "image_prompt": "Cena A"}]}
        install_scene_director_active_patch(Generator)
        with patch("app.services.scene_director_active.direct_scene_plan", side_effect=ValueError("director unavailable")):
            result = Generator().create_video_from_plan(original)
        self.assertIs(result["received_plan"], original)
        self.assertEqual(result["scene_director"]["fallback"], "original_plan")
        self.assertEqual(original["scenes"][0]["narration"], "Narracao integral.")

    def test_render_failure_must_propagate_even_after_director_fallback(self):
        class Generator:
            def create_video_from_plan(self, plan):
                raise RuntimeError("renderer unavailable")

        install_scene_director_active_patch(Generator)
        with patch("app.services.scene_director_active.direct_scene_plan", side_effect=ValueError("director unavailable")):
            with self.assertRaisesRegex(RuntimeError, "renderer unavailable"):
                Generator().create_video_from_plan({"scenes": []})

    def test_director_success_preserves_report_and_original_plan(self):
        class Generator:
            def create_video_from_plan(self, plan):
                return {"received_plan": plan}

        original = {"scenes": [{"narration": "Texto completo.", "image_prompt": "Original"}]}
        modified = {"scenes": [{"narration": "Texto completo.", "image_prompt": "Novo enquadramento"}]}
        install_scene_director_active_patch(Generator)
        with patch("app.services.scene_director_active.direct_scene_plan", return_value=(modified, {"mode": "active"})):
            result = Generator().create_video_from_plan(original)
        self.assertIs(result["received_plan"], modified)
        self.assertEqual(result["scene_director"]["mode"], "active")
        self.assertEqual(original["scenes"][0]["image_prompt"], "Original")


if __name__ == "__main__":
    unittest.main()
