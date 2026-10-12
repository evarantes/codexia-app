"""Duration is a hard minimum for the legacy story-text review flow."""
import json
import unittest
from unittest.mock import Mock

from app.services.story_review_editor import (
    _meet_minimum_narration_words,
    _narrated_word_count,
)


def sections(words_each):
    text = " ".join(["Deus sustenta nossa esperança e fortalece nosso caminho."] * words_each)
    return {k: text for k in (
        "hook", "development", "central_truth", "transformation",
        "application", "climax", "reflection",
    )}


class LegacyDurationFloorTests(unittest.TestCase):
    def test_short_generation_is_expanded_before_being_accepted(self):
        initial = {"title": "Vida e Esperança", "sections": sections(1)}
        extended = {"title": "Vida e Esperança", "sections": sections(14)}
        provider = Mock()
        provider._generate_text.return_value = json.dumps(extended, ensure_ascii=False)
        result, narration = _meet_minimum_narration_words(
            provider, initial, 500, instruction="Tema cristão de esperança"
        )
        self.assertEqual(result["sections"], extended["sections"])
        self.assertGreaterEqual(_narrated_word_count(narration), 500)

    def test_failed_or_short_expansion_must_not_be_reported_as_long(self):
        initial = {"title": "Vida e Esperança", "sections": sections(1)}
        provider = Mock()
        provider._generate_text.return_value = json.dumps(initial, ensure_ascii=False)
        _, narration = _meet_minimum_narration_words(
            provider, initial, 200, instruction="Tema cristão", max_expansions=2
        )
        self.assertLess(_narrated_word_count(narration), 200)
        self.assertEqual(provider._generate_text.call_count, 2)


if __name__ == "__main__":
    unittest.main()
