"""Private four-segment audio review contracts, without provider calls."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.entre_nos_dois_audio_review import (
    StudioAudioError, audio_file, list_segment_audio, store_segment_audio, preview_audio_timeline,
)


class PrivatePilotAudioTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"CODEXIA_STUDIO_PRIVATE_DIR": folder.name})
        env.start()
        self.addCleanup(env.stop)

    def test_segment_is_private_and_isolated_by_user(self):
        with patch("app.services.entre_nos_dois_audio_review._duration_seconds", return_value=2.5):
            result = store_segment_audio(user_id=42, index=1, body=b"private-test-audio")
            self.assertEqual(result["speaker"], "Helena")
            self.assertTrue(result["private"])
            self.assertFalse(result["auto_publish"])
            self.assertTrue(audio_file(user_id=42, index=1).is_file())
            self.assertFalse(audio_file(user_id=43, index=1).exists())
            self.assertTrue(list_segment_audio(user_id=42)["segments"][0]["uploaded"])
            self.assertFalse(list_segment_audio(user_id=43)["segments"][0]["uploaded"])

    def test_all_four_segments_have_correct_speakers(self):
        status = list_segment_audio(user_id=42)
        self.assertEqual([x["speaker"] for x in status["segments"]], [
            "Helena", "Gabriel", "Helena", "Gabriel",
        ])
        self.assertFalse(status["all_uploaded"])

    def test_timeline_waits_for_all_four_mp3s(self):
        timeline = preview_audio_timeline(user_id=42)
        self.assertFalse(timeline["ready"])
        self.assertEqual(timeline["missing_indices"], [1, 2, 3, 4])
        self.assertIsNone(timeline["total_seconds"])
        self.assertFalse(timeline["auto_publish"])

    def test_timeline_uses_measured_durations_and_pauses(self):
        with patch("app.services.entre_nos_dois_audio_review._duration_seconds", return_value=2.5):
            for index in (1, 2, 3, 4):
                store_segment_audio(user_id=42, index=index, body=b"fixture-audio")
            timeline = preview_audio_timeline(user_id=42)
        self.assertTrue(timeline["ready"])
        self.assertEqual(len(timeline["segments"]), 4)
        self.assertEqual(timeline["total_seconds"], 11.05)
        self.assertEqual(timeline["segments"][0]["start_seconds"], 0.0)
        self.assertEqual(timeline["segments"][1]["start_seconds"], 2.85)
        self.assertFalse(timeline["is_rendered_video"])
        self.assertFalse(timeline["auto_publish"])
        self.assertFalse(preview_audio_timeline(user_id=43)["ready"])

    def test_reject_invalid_audio_or_index(self):
        with self.assertRaises(StudioAudioError):
            store_segment_audio(user_id=42, index=1, body=b"junk")
        for value in (0, -1, 5, True):
            with self.subTest(value=value), self.assertRaises(StudioAudioError):
                audio_file(user_id=42, index=value)
        with self.assertRaises(StudioAudioError):
            store_segment_audio(user_id=42, index=1, body=b"x" * (10 * 1024 * 1024 + 1))

    def test_no_directory_is_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "app.services.entre_nos_dois_audio_review._duration_seconds", return_value=2.5,
        ):
            with self.assertRaises(StudioAudioError):
                store_segment_audio(user_id=42, index=1, body=b"example")


if __name__ == "__main__":
    unittest.main()
