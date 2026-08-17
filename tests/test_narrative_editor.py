from __future__ import annotations

import os
import unittest
from unittest import mock

from app.services import narrative_editor as editor


class _FakeGenerator:
    def __init__(self):
        self.ai_service = type("AI", (), {"ai_task_id": None})()
        self.received_plan = None

    def create_video_from_plan(self, plan, *args, **kwargs):
        self.received_plan = plan
        return {"file_path": "/tmp/fake.mp4", "render_report": {}}


class NarrativeEditorTests(unittest.TestCase):
    def tearDown(self):
        for key in ("ENABLE_NARRATIVE_EDITOR", "NARRATIVE_EDITOR_MODEL"):
            os.environ.pop(key, None)

    def test_analysis_flags_missing_title_and_weak_arc(self):
        report = editor.analyze_narrative_plan({
            "scenes": [
                {"text": "Uma frase curta."},
                {"text": "Outra frase curta."},
            ]
        })
        self.assertFalse(report["has_title"])
        self.assertIn("missing_title", report["issues"])
        self.assertIn("too_few_scenes_for_clear_arc", report["issues"])

    def test_analysis_flags_repetitive_voce_ja_hook(self):
        report = editor.analyze_narrative_plan({
            "title": "Teste",
            "scenes": [
                {"text": "Você já se sentiu sozinho mesmo cercado de pessoas? Esta abertura precisa variar."},
                {"text": "O desenvolvimento segue com conteúdo suficiente para a mensagem."},
                {"text": "O encerramento fecha a ideia de forma clara para o ouvinte."},
            ],
        })
        self.assertIn("repetitive_voce_ja_hook", report["issues"])

    def test_quality_guard_repairs_dangling_opening_and_voce_ja(self):
        repaired, guard = editor._quality_guard_texts([
            "Você já se sentiu sozinho? Uma mensagem de... Deus conhece o seu coração.",
            "Continue firme.",
            "A esperança permanece.",
        ])
        self.assertFalse(repaired[0].lower().startswith("você já"))
        self.assertNotIn("Uma mensagem de", repaired[0])
        self.assertTrue(guard["repetitive_hook_repaired"])
        self.assertGreaterEqual(guard["dangling_phrase_repairs"], 1)

    def test_patch_guarantees_title_even_if_ai_is_unavailable(self):
        cls = type("NarrativeFallbackGenerator", (_FakeGenerator,), {})
        editor.install_narrative_editor_patch(cls)
        instance = cls()
        plan = {
            "topic": "Esperança em tempos difíceis",
            "scenes": [
                {"text": "Começamos reconhecendo a dificuldade."},
                {"text": "No caminho, encontramos razões para perseverar."},
                {"text": "Ao final, entendemos que a esperança pode permanecer."},
            ],
        }
        with mock.patch.object(editor, "revise_plan_with_ai", return_value=(None, {"changed": False, "error": "offline"})):
            result = instance.create_video_from_plan(plan)
        self.assertEqual(instance.received_plan["title"], "Esperança em tempos difíceis")
        self.assertIn("narrative_editor", result)
        self.assertEqual(len(instance.received_plan["scenes"]), 3)
        self.assertTrue(instance.received_plan["final_message"])

    def test_valid_ai_revision_preserves_scene_count_and_visual_fields(self):
        cls = type("NarrativeRevisionGenerator", (_FakeGenerator,), {})
        editor.install_narrative_editor_patch(cls)
        instance = cls()
        plan = {
            "title": "Antigo",
            "scenes": [
                {"text": "Inicio original com contexto suficiente para a cena.", "image_prompt": "visual A"},
                {"text": "Meio original desenvolvendo a ideia principal com clareza.", "image_prompt": "visual B"},
                {"text": "Fim original encerrando a mensagem com uma reflexão.", "image_prompt": "visual C"},
            ],
        }
        revised = {
            "title": "Novo título",
            "scenes": [
                {"text": "Uma abertura mais natural e envolvente apresenta a ideia central."},
                {"text": "O desenvolvimento conecta os argumentos e conduz a reflexão sem repetições."},
                {"text": "A conclusão fecha o raciocínio e deixa uma mensagem memorável ao ouvinte."},
            ],
            "closing_message": "Jesus continua presente. Leve esta esperança com você.",
        }
        with mock.patch.object(editor, "revise_plan_with_ai", return_value=(revised, {"changed": True})):
            instance.create_video_from_plan(plan)
        self.assertEqual(instance.received_plan["title"], "Novo título")
        self.assertEqual(len(instance.received_plan["scenes"]), 3)
        self.assertEqual(instance.received_plan["closing_message"], instance.received_plan["final_message"])

    def test_explicit_disable_preserves_original_plan_except_required_title_fallback(self):
        os.environ["ENABLE_NARRATIVE_EDITOR"] = "false"
        report = editor.analyze_narrative_plan({"title": "T", "scenes": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        self.assertTrue(report["has_title"])
        self.assertEqual(report["scene_count"], 3)


if __name__ == "__main__":
    unittest.main()
