import tempfile
import unittest
from pathlib import Path

from app.services.entre_nos_dois_timeline import (
    DialogueTimelineError, build_measured_timeline,
)
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class MeasuredDialogueTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.files = []
        for index in range(1, 5):
            p = Path(self.temp.name) / f"{index:02d}.mp3"
            p.write_bytes(b"ID3" + b"0" * 2048)
            self.files.append(p)
        plan = prepare_speech_manifest()
        self.audio = {
            "program": "entre-nos-dois",
            "project_id": 42,
            "approval_status": "awaiting_human_review",
            "segments": [
                {"index": s["index"], "speaker": s["speaker"], "path": str(path)}
                for s, path in zip(plan["segments"], self.files)
            ],
        }

    def test_timeline_uses_real_durations_and_preserves_voices(self):
        durations = iter((6.0, 8.0, 7.5, 5.0))
        result = build_measured_timeline(self.audio, probe=lambda path: next(durations))
        self.assertEqual(result["measured_total_seconds"], 27.55)
        self.assertEqual(result["segments"][1]["start_seconds"], 6.35)
        self.assertEqual(result["segments"][0]["speaker"], "Helena")
        self.assertEqual(result["segments"][1]["speaker"], "Gabriel")
        self.assertFalse(result["auto_publish"])
        self.assertFalse(result["video_generated"])

    def test_missing_audio_is_rejected(self):
        self.files[0].unlink()
        with self.assertRaises(DialogueTimelineError):
            build_measured_timeline(self.audio, probe=lambda path: 5.0)

    def test_repeated_audio_is_rejected(self):
        self.audio["segments"][1]["path"] = str(self.files[0])
        with self.assertRaises(DialogueTimelineError):
            build_measured_timeline(self.audio, probe=lambda path: 5.0)

    def test_wrong_speaker_is_rejected(self):
        self.audio["segments"][0]["speaker"] = "Gabriel"
        with self.assertRaises(DialogueTimelineError):
            build_measured_timeline(self.audio, probe=lambda path: 5.0)

    def test_invalid_duration_is_rejected(self):
        with self.assertRaises(DialogueTimelineError):
            build_measured_timeline(self.audio, probe=lambda path: float("nan"))


if __name__ == "__main__":
    unittest.main()
