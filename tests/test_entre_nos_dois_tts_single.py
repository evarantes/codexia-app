import os
import tempfile
import unittest
from unittest.mock import patch

from app.services.entre_nos_dois_cost_worksheet import worksheet
from app.services.entre_nos_dois_tts_single import PaidPilotTtsError, submit_one_turn
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class SingleTurnPaidTtsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {
            "CODEXIA_STUDIO_PRIVATE_DIR": folder.name,
            "CODEXIA_STUDIO_ENABLE_PAID_TTS": "1",
            "CODEXIA_STUDIO_MAX_TTS_USD": "1.00",
        })
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.audio = b"ID3" + b"x" * 2048

    def _submit(self, **overrides):
        args = {
            "user_id": 42,
            "index": 1,
            "api_key": "temporary-test-only-key",
            "quote_digest": worksheet()["manifest_sha256"],
            "usd_per_1000_characters": "0.25",
            "approved_max_usd": "0.10",
            "confirmation": "AUTORIZO FALA 1",
            "rate_checked_in_provider_account": True,
            "verify_voices": lambda **kw: {
                "all_accessible": True,
                "checked": [{"speaker": speaker, "voice_id": voice} for speaker, voice in
                            prepare_speech_manifest()["approved_voices"].items()],
            },
            "post_audio": lambda url, body, key: self.calls.append((url, body)) or self.audio,
        }
        args.update(overrides)
        return submit_one_turn(**args)

    def test_paid_feature_is_disabled_by_default(self):
        with patch.dict(os.environ, {"CODEXIA_STUDIO_ENABLE_PAID_TTS": "0"}):
            with self.assertRaises(PaidPilotTtsError):
                self._submit()
        self.assertEqual(self.calls, [])

    def test_exact_approval_and_quote_digest_are_required(self):
        for override in (
            {"confirmation": "yes"},
            {"rate_checked_in_provider_account": False},
            {"quote_digest": "obsolete"},
            {"approved_max_usd": "2.00"},
            {"approved_max_usd": "0.0001"},
            {"usd_per_1000_characters": "NaN"},
        ):
            with self.subTest(override=override):
                with self.assertRaises(PaidPilotTtsError):
                    self._submit(**override)
        self.assertEqual(self.calls, [])

    def test_creates_single_mp3_and_never_charges_twice(self):
        with patch("app.services.entre_nos_dois_audio_review._duration_seconds", return_value=2.1):
            result = self._submit()
            self.assertEqual(result["index"], 1)
            self.assertEqual(result["status"], "awaiting_human_audio_review")
            self.assertEqual(len(self.calls), 1)
            with self.assertRaises(PaidPilotTtsError):
                self._submit()
        self.assertEqual(len(self.calls), 1)

    def test_ambiguous_paid_failure_leaves_durable_reservation(self):
        def uncertain(*_):
            self.calls.append("paid_attempt")
            raise TimeoutError()
        with self.assertRaises(PaidPilotTtsError):
            self._submit(post_audio=uncertain)
        from app.services.entre_nos_dois_audio_review import audio_file
        path = audio_file(user_id=42, index=1).with_suffix(".paid-request-pending")
        self.assertTrue(path.exists())
        with self.assertRaises(PaidPilotTtsError):
            self._submit()
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
