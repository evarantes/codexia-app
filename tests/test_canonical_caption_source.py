from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.canonical_caption_source import install_canonical_caption_source_patch


class _BaseGenerator:
    def __init__(self):
        self.ai_service = None

    def _normalize_tts_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _caption_timeline_from_text(self, narration: str, duration: float):
        words = str(narration or "").split()
        if not words:
            return []
        middle = max(1, len(words) // 2)
        return [
            {"caption": " ".join(words[:middle]), "start": 0.0, "end": duration / 2.0},
            {"caption": " ".join(words[middle:]), "start": duration / 2.0, "end": duration},
        ]


class _DummyGenerator(_BaseGenerator):
    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        return {
            "source": "real_segments_aligned_to_narration",
            "timeline": [
                {"caption": "Jesus é o centro", "start": 0.0, "end": 1.8},
                {"caption": "de nossa vida", "start": 1.8, "end": 3.6},
            ],
        }


class _AlreadyMatchingGenerator(_BaseGenerator):
    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        return {
            "source": "text_fallback",
            "timeline": [
                {"caption": "Jesus é o centro", "start": 0.0, "end": 1.5},
                {"caption": "da nossa existência.", "start": 1.5, "end": 3.2},
            ],
        }


class _CheckpointGenerator(_BaseGenerator):
    def __init__(self):
        super().__init__()
        self.ai_service = SimpleNamespace(ai_task_id="task-improved-text")
        self.builder_narration = None

    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        self.builder_narration = narration
        return {
            "source": "real_segments_aligned_to_narration",
            "timeline": [
                {"caption": "Jesus não é apenas um nome", "start": 0.0, "end": 2.0},
                {"caption": "ele muda a vida", "start": 2.0, "end": 4.0},
            ],
        }


class _BrokenBuilderGenerator(_BaseGenerator):
    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        raise RuntimeError("ASR indisponível")


class _EmptyBuilderGenerator(_BaseGenerator):
    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path=None):
        return {"source": "real_segments_aligned_to_narration", "timeline": []}

    def _caption_timeline_from_text(self, narration: str, duration: float):
        # Simula um helper futuro/legado que também não consegue produzir blocos.
        return []


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
        audit = generator._codexia_canonical_narration_integrity
        self.assertTrue(audit["captions_match_after"])
        self.assertTrue(audit["auto_repaired"])
        self.assertEqual(audit["repair_mode"], "preserved_asr_timestamps")

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
            resolved = generator._codexia_resolve_canonical_narration_text(old_renderer_text)
            details = generator._build_caption_timeline_details(old_renderer_text, 4.0, audio_path="fake.mp3")

        self.assertEqual(resolved, improved_text_sent_to_tts)
        self.assertEqual(generator.builder_narration, improved_text_sent_to_tts)
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
        audit = generator._codexia_canonical_narration_integrity
        self.assertTrue(audit["captions_matched_before"])
        self.assertTrue(audit["captions_match_after"])
        self.assertFalse(audit["auto_repaired"])

    def test_builder_exception_becomes_local_caption_recovery_not_video_failure(self):
        cls = install_canonical_caption_source_patch(_BrokenBuilderGenerator)
        generator = cls()
        narration = "Jesus permanece fiel do início ao fim."

        details = generator._build_caption_timeline_details(narration, 6.0, audio_path="fake.mp3")

        joined = " ".join(item["caption"] for item in details["timeline"] if item.get("caption")).strip()
        self.assertEqual(joined, narration)
        audit = generator._codexia_canonical_narration_integrity
        self.assertTrue(audit["captions_match_after"])
        self.assertEqual(audit["builder_error"], "ASR indisponível")
        self.assertIn(audit["repair_mode"], {"canonical_text_timeline", "canonical_single_block"})

    def test_even_empty_builder_and_empty_local_timeline_falls_back_to_single_block(self):
        cls = install_canonical_caption_source_patch(_EmptyBuilderGenerator)
        generator = cls()
        narration = "A graça de Jesus sustenta a nossa fé."

        details = generator._build_caption_timeline_details(narration, 5.0, audio_path="fake.mp3")

        self.assertEqual(len(details["timeline"]), 1)
        self.assertEqual(details["timeline"][0]["caption"], narration)
        self.assertEqual(details["timeline"][0]["source"], "canonical_single_block")
        self.assertEqual(generator._codexia_canonical_narration_integrity["repair_mode"], "canonical_single_block")
        self.assertTrue(generator._codexia_canonical_narration_integrity["captions_match_after"])

    def test_force_helper_supports_opening_silence_without_changing_text(self):
        cls = install_canonical_caption_source_patch(_DummyGenerator)
        generator = cls()
        narration = "Jesus é o princípio e o fim."

        timeline = generator._codexia_force_canonical_caption_timeline(
            narration,
            8.0,
            timeline=[],
            opening_silence_sec=0.45,
        )

        joined = " ".join(item["caption"] for item in timeline if item.get("caption")).strip()
        self.assertEqual(joined, narration)
        self.assertGreaterEqual(timeline[0]["start"], 0.45)

    def test_patch_is_idempotent_and_exposes_v4_contract(self):
        cls = install_canonical_caption_source_patch(_DummyGenerator)
        wrapped = cls._build_caption_timeline_details
        resolver = cls._codexia_resolve_canonical_narration_text
        repairer = cls._codexia_force_canonical_caption_timeline
        cls = install_canonical_caption_source_patch(cls)
        self.assertIs(cls._build_caption_timeline_details, wrapped)
        self.assertIs(cls._codexia_resolve_canonical_narration_text, resolver)
        self.assertIs(cls._codexia_force_canonical_caption_timeline, repairer)
        self.assertEqual(cls._codexia_caption_integrity_version, 4)


if __name__ == "__main__":
    unittest.main()
