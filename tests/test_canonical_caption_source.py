from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.canonical_caption_source import install_canonical_caption_source_patch


class _DummyGenerator:
    def __init__(self):
        self.ai_service = None

    def _normalize_tts_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        return {
            "source": "real_segments_aligned_to_narration",
            "timeline": [
                {"caption": "Jesus é o centro", "start": 0.0, "end": 1.8},
                {"caption": "de nossa vida", "start": 1.8, "end": 3.6},
            ],
        }


class _AlreadyMatchingGenerator:
    def __init__(self):
        self.ai_service = None

    def _normalize_tts_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        return {
            "source": "text_fallback",
            "timeline": [
                {"caption": "Jesus é o centro", "start": 0.0, "end": 1.5},
                {"caption": "da nossa existência.", "start": 1.5, "end": 3.2},
            ],
        }


class _CheckpointGenerator:
    def __init__(self):
        self.ai_service = SimpleNamespace(ai_task_id="task-improved-text")

    def _normalize_tts_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        # Simula ASR devolvendo uma terceira variante textual.
        return {
            "source": "real_segments_aligned_to_narration",
            "timeline": [
                {"caption": "Jesus não é apenas um nome", "start": 0.0, "end": 2.0},
                {"caption": "ele muda a vida", "start": 2.0, "end": 4.0},
            ],
        }


class CanonicalCaptionSourceTests(unittest.TestCase):
    def test_transcription_text_is_replaced_but_timings_are_preserved(self):
        cls = install_canonical_caption_source_patch(_DummyGenerator)
        generator = cls()
        narration = "Jesus é o centro da nossa existência."

        details = generator._build_caption_timeline_details(narration, 3.6, audio_path="fake.mp3")
        timeline = details["timeline"]

        self.assertEqual(details["source"], "real_segments_aligned_to_narration")
        self.assertEqual(details["timing_source"], "real_segments_aligned_to_narration")
        self.assertTrue(details["canonical_text_remapped"])
        self.assertEqual(timeline[0]["start"], 0.0)
        self.assertEqual(timeline[0]["end"], 1.8)
        self.assertEqual(timeline[1]["start"], 1.8)
        self.assertEqual(timeline[1]["end"], 3.6)
        joined = " ".join(item["caption"] for item in timeline if item.get("caption")).strip()
        self.assertEqual(joined, narration)
        self.assertTrue(generator._codexia_canonical_narration_integrity["captions_match_after"])
        self.assertTrue(generator._codexia_canonical_narration_integrity["remapped_from_transcription_text"])

    def test_tts_checkpoint_is_authority_even_if_renderer_argument_is_old_text(self):
        cls = install_canonical_caption_source_patch(_CheckpointGenerator)
        generator = cls()
        old_renderer_text = "Texto anterior antes de clicar em Melhorar."
        improved_text_sent_to_tts = "Jesus é o centro da sua vida e transforma cada decisão."
        fake_task = {
            "result": {
                "audio_checkpoint": {
                    "final_text_sent_to_tts": improved_text_sent_to_tts,
                }
            }
        }

        with patch("app.services.task_manager.get_task", return_value=fake_task), patch(
            "app.services.task_manager.merge_task_result", return_value=fake_task
        ):
            details = generator._build_caption_timeline_details(old_renderer_text, 4.0, audio_path="fake.mp3")

        joined = " ".join(item["caption"] for item in details["timeline"] if item.get("caption")).strip()
        self.assertEqual(joined, improved_text_sent_to_tts)
        audit = generator._codexia_canonical_narration_integrity
        self.assertEqual(audit["canonical_text_source"], "audio_checkpoint.final_text_sent_to_tts")
        self.assertTrue(audit["captions_match_after"])

    def test_matching_fallback_is_left_unchanged(self):
        cls = install_canonical_caption_source_patch(_AlreadyMatchingGenerator)
        generator = cls()
        narration = "Jesus é o centro da nossa existência."

        details = generator._build_caption_timeline_details(narration, 3.2)

        self.assertEqual(details["source"], "text_fallback")
        self.assertNotIn("canonical_text_remapped", details)
        joined = " ".join(item["caption"] for item in details["timeline"]).strip()
        self.assertEqual(joined, narration)
        self.assertTrue(generator._codexia_canonical_narration_integrity["captions_matched_before"])
        self.assertTrue(generator._codexia_canonical_narration_integrity["captions_match_after"])

    def test_patch_is_idempotent(self):
        cls = install_canonical_caption_source_patch(_DummyGenerator)
        wrapped = cls._build_caption_timeline_details
        cls = install_canonical_caption_source_patch(cls)
        self.assertIs(cls._build_caption_timeline_details, wrapped)


if __name__ == "__main__":
    unittest.main()
