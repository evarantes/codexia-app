from __future__ import annotations

import os
import unittest

from app.services.scene_director_active import direct_scene_plan


class SceneDirectorActiveTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ENABLE_SCENE_DIRECTOR", None)

    def test_director_changes_only_visual_prompt_and_preserves_story_contract(self):
        plan = {
            "title": "Teste",
            "scenes": [
                {"text": "Mesmo na tempestade, continue firme.", "image_prompt": "Jesus comforting a worried person"},
                {"text": "O barco encontra direção.", "image_prompt": "Jesus comforting a worried person"},
            ],
        }
        directed, report = direct_scene_plan(plan)

        self.assertEqual(len(directed["scenes"]), 2)
        self.assertEqual(directed["scenes"][0]["text"], plan["scenes"][0]["text"])
        self.assertEqual(directed["scenes"][1]["text"], plan["scenes"][1]["text"])
        self.assertNotEqual(directed["scenes"][0]["image_prompt"], plan["scenes"][0]["image_prompt"])
        self.assertNotEqual(directed["scenes"][1]["image_prompt"], plan["scenes"][1]["image_prompt"])
        self.assertIn("Camera direction:", directed["scenes"][0]["image_prompt"])
        self.assertIn("storm", directed["scenes"][0]["image_prompt"].lower())
        self.assertIn("boat", directed["scenes"][1]["image_prompt"].lower())
        self.assertGreaterEqual(report["mutated_scene_count"], 2)
        self.assertFalse(report["changes_narration"])
        self.assertFalse(report["changes_scene_count"])
        self.assertNotEqual(report["directives"][0]["shot"], report["directives"][1]["shot"])
        self.assertNotEqual(report["directives"][0]["visual_role"], report["directives"][1]["visual_role"])

    def test_duplicate_prompt_gets_hard_anti_repetition_instruction(self):
        plan = {
            "scenes": [
                {"text": "Primeiro momento", "image_prompt": "Jesus talks with a sad woman in a warm biblical room"},
                {"text": "Segundo momento", "image_prompt": "Jesus talks with a sad woman in a warm biblical room"},
                {"text": "A esperança surge na luz", "image_prompt": "Jesus talks with a sad woman in a warm biblical room"},
            ]
        }
        directed, report = direct_scene_plan(plan)
        self.assertGreaterEqual(report["directives"][1]["previous_prompt_similarity"], 0.72)
        self.assertTrue(report["directives"][1]["anti_repetition"])
        self.assertIn("do NOT reuse the same room", directed["scenes"][1]["image_prompt"])
        self.assertGreaterEqual(report["anti_repetition_interventions"], 1)
        self.assertIn("symbolic", report["directives"][2]["visual_role"])

    def test_symbolic_scene_can_decenter_recurring_characters(self):
        plan = {
            "scenes": [
                {"text": "A solidão parece enorme.", "image_prompt": "Jesus beside a sad person"},
                {"text": "O caminho continua.", "image_prompt": "Jesus beside a sad person"},
                {"text": "Uma luz de esperança aparece.", "image_prompt": "Jesus beside a sad person"},
            ]
        }
        directed, _ = direct_scene_plan(plan)
        symbolic_prompt = directed["scenes"][2]["image_prompt"]
        self.assertIn("Omit nonessential characters", symbolic_prompt)
        self.assertIn("dawn light", symbolic_prompt)

    def test_explicit_disable_is_immediate_rollback(self):
        os.environ["ENABLE_SCENE_DIRECTOR"] = "false"
        plan = {"scenes": [{"text": "Teste", "image_prompt": "original prompt"}]}
        directed, report = direct_scene_plan(plan)
        self.assertIs(directed, plan)
        self.assertFalse(report["enabled"])
        self.assertEqual(report["mode"], "disabled")
        self.assertEqual(plan["scenes"][0]["image_prompt"], "original prompt")


if __name__ == "__main__":
    unittest.main()
