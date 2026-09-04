import unittest

from app.services.global_narrative_entrypoint import prepare_global_narrative_request
from app.services.narrative_structure_standard import NARRATIVE_STRUCTURE_STANDARD_VERSION


class GlobalNarrativeEntrypointsV2Tests(unittest.TestCase):
    def assert_global(self, payload):
        prepared = prepare_global_narrative_request(payload)
        self.assertTrue(prepared["narrative_structure_applied"])
        self.assertEqual(prepared["narrative_standard_scope"], "global")
        self.assertEqual(prepared["narrative_standard_version"], NARRATIVE_STRUCTURE_STANDARD_VERSION)
        self.assertEqual(
            prepared["narrative_reference_style"],
            "jesus_o_motivo_de_eu_existir_without_copying",
        )
        prompt = prepared["narrative_structure_prompt"]
        self.assertIn("Jesus, o motivo de eu existir", prompt)
        self.assertIn("Nunca copie", prompt)

    def test_story_devotional_uses_global_standard(self):
        self.assert_global(
            {"source_module": "story_devotional_editor", "kind": "devotional", "topic": "Jesus, o Justo"}
        )

    def test_youtube_auto_uses_global_standard(self):
        self.assert_global(
            {"source_module": "youtube_auto_manual", "kind": "story", "topic": "A justiça de Jesus"}
        )

    def test_scheduled_series_uses_global_standard(self):
        self.assert_global(
            {"source_module": "scheduled_series", "kind": "story", "topic": "Daniel permanece fiel"}
        )

    def test_automatic_planning_uses_global_standard(self):
        self.assert_global(
            {"source_module": "automatic_content_planning", "kind": "devotional", "topic": "Deus não esqueceu de você"}
        )

    def test_shorts_keep_their_own_contract(self):
        payload = {"source_module": "short", "kind": "short", "topic": "Esperança"}
        self.assertEqual(prepare_global_narrative_request(payload), payload)


if __name__ == "__main__":
    unittest.main()
