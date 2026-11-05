import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.services.entre_nos_dois_tts_adapter import (
    SpeechGenerationError,
    generate_pilot_audio,
)
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class ApprovedTTSAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manifest = prepare_speech_manifest()
        self.limit = self.manifest["estimated_characters"]
        self.mock_post = Mock(return_value=b"ID3" + b"0" * 2048)

    def invoke(self, **overrides):
        args = dict(
            output_directory=self.temp.name,
            api_key="fake-secret",
            approved_max_characters=self.limit,
            project_id=42,
            manifest=self.manifest,
            post_audio=self.mock_post,
            verify_voices=lambda **kwargs: {
                "all_accessible": True,
                "checked": [
                    {"speaker": speaker, "voice_id": voice_id}
                    for speaker, voice_id in self.manifest["approved_voices"].items()
                ],
            },
        )
        args.update(overrides)
        return generate_pilot_audio(**args)

    def test_generates_per_speaker_with_approved_voice_ids(self):
        result = self.invoke()
        self.assertEqual(len(result["segments"]), 4)
        self.assertEqual(self.mock_post.call_count, 4)
        self.assertEqual(result["approval_status"], "awaiting_human_review")
        self.assertFalse(result["auto_publish"])
        urls = [call.args[0] for call in self.mock_post.call_args_list]
        self.assertIn("EMGk4qRFrRTnkdPoTzu3", urls[0])
        self.assertIn("ceOf6Sb1xKhC0ZVxHIzb", urls[1])
        self.assertTrue(all(Path(s["path"]).is_file() for s in result["segments"]))

    def test_reuses_audio_without_new_billable_calls(self):
        self.invoke()
        result = self.invoke()
        self.assertEqual(self.mock_post.call_count, 4)
        self.assertTrue(all(s["reused"] for s in result["segments"]))

    def test_invalid_cache_is_not_silently_reused_or_charged_again(self):
        first = self.invoke()
        Path(first["segments"][0]["path"]).write_bytes(b"invalid preexisting data" * 200)
        self.mock_post.reset_mock()
        with self.assertRaises(SpeechGenerationError):
            self.invoke()
        self.mock_post.assert_not_called()

    def test_output_filenames_identify_different_voices(self):
        result = self.invoke()
        names = [Path(seg["path"]).name for seg in result["segments"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(len(name.rsplit("_", 1)[-1]) == 20 for name in names))

    def test_rejects_unapproved_budget_before_network(self):
        with self.assertRaises(ValueError):
            self.invoke(approved_max_characters=self.limit - 1)
        self.mock_post.assert_not_called()

    def test_rejects_changed_voices_before_network(self):
        manifest = prepare_speech_manifest()
        manifest["segments"][0]["voice_id"] = "unapproved"
        with self.assertRaises(ValueError):
            self.invoke(manifest=manifest)
        self.mock_post.assert_not_called()

    def test_preflight_blocks_billing(self):
        with self.assertRaises(SpeechGenerationError):
            self.invoke(verify_voices=lambda **kwargs: {"all_accessible": False, "checked": []})
        self.mock_post.assert_not_called()

    def test_preflight_requires_exact_voices(self):
        with self.assertRaises(SpeechGenerationError):
            self.invoke(verify_voices=lambda **kwargs: {
                "all_accessible": True,
                "checked": [
                    {"speaker": "Gabriel", "voice_id": "wrong-id"},
                    {"speaker": "Helena", "voice_id": self.manifest["approved_voices"]["Helena"]},
                ],
            })
        self.mock_post.assert_not_called()

    def test_stops_at_first_invalid_audio(self):
        self.mock_post.return_value = b"not an mp3"
        with self.assertRaises(SpeechGenerationError):
            self.invoke()
        self.assertEqual(self.mock_post.call_count, 1)
        self.assertFalse(list(Path(self.temp.name).rglob("*.mp3")))


if __name__ == "__main__":
    unittest.main()
