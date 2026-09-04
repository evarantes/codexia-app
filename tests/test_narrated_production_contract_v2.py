from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.narrated_production_contract import (
    apply_approved_audio_as_source_of_truth,
    build_narration_review_state,
    preserve_approved_audio,
    validate_spoken_text,
)


class NarratedProductionContractV2Tests(unittest.TestCase):
    def test_rejects_technical_text_before_tts(self):
        with self.assertRaises(ValueError):
            validate_spoken_text('CENA 1 prompt: {"text": "Jesus é justo"}')

    def test_review_state_waits_for_human_approval(self):
        state = build_narration_review_state(
            spoken_text="Jesus nos chama a viver a justiça com verdade e misericórdia.",
            audio_url="/media/narration-v1.mp3",
            duration_seconds=18.4,
        )
        self.assertEqual(state["status"], "awaiting_narration_review")
        self.assertFalse(state["approved"])
        self.assertEqual(state["next_action"], "approve_or_rebuild_narration")

    def test_approved_mp3_is_copied_to_task_and_becomes_duration_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "preview.mp3"
            source.write_bytes(b"ID3" + b"audio" * 100)
            task_dir = root / "task-123"

            approved = preserve_approved_audio(
                source_path=str(source),
                task_dir=str(task_dir),
                spoken_text="Jesus é o padrão de justiça que transforma nossas escolhas diárias.",
                duration_seconds=34.25,
            )
            source.unlink()

            self.assertTrue(Path(approved.path).exists())
            plan = apply_approved_audio_as_source_of_truth(
                {"duration_seconds": 15, "duration_source": "requested"},
                approved,
            )
            self.assertEqual(plan["seed_audio_path"], approved.path)
            self.assertEqual(plan["approved_audio_duration_seconds"], 34.25)
            self.assertEqual(plan["render_target_duration_seconds"], 34.25)
            self.assertEqual(plan["duration_source"], "approved_audio")
            self.assertTrue(plan["tts_locked"])
            self.assertFalse(plan["allow_tts_generation"])

    def test_audio_feedback_is_preserved_for_rebuild_request(self):
        state = build_narration_review_state(
            spoken_text="Uma narração humana e clara.",
            audio_url="/media/narration.mp3",
            duration_seconds=10.0,
            feedback="Fale mais devagar e com mais emoção no fechamento.",
            version=2,
        )
        self.assertIn("mais devagar", state["feedback"])
        self.assertEqual(state["version"], 2)


if __name__ == "__main__":
    unittest.main()
