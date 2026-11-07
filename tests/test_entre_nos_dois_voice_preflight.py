import unittest
from unittest.mock import Mock

from app.services.entre_nos_dois_voice_preflight import (
    VoiceAccessError,
    check_approved_voices,
)


class VoicePreflightTests(unittest.TestCase):
    def test_verified_ids_without_tts_calls(self):
        callback = Mock(side_effect=lambda voice_id, key: {
            "voice_id": voice_id, "name": "voice metadata",
        })
        result = check_approved_voices(api_key="test-key", fetch_metadata=callback)
        self.assertTrue(result["all_accessible"])
        self.assertFalse(result["billable_generation_performed"])
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(
            [v["speaker"] for v in result["checked"]], ["Gabriel", "Helena"]
        )

    def test_refuses_mismatched_voice_identifier(self):
        callback = Mock(return_value={"voice_id": "different-voice"})
        with self.assertRaises(VoiceAccessError):
            check_approved_voices(api_key="test-key", fetch_metadata=callback)
        self.assertEqual(callback.call_count, 1)

    def test_refuses_missing_key_without_call(self):
        callback = Mock()
        with self.assertRaises(ValueError):
            check_approved_voices(api_key="", fetch_metadata=callback)
        callback.assert_not_called()

    def test_refuses_unavailable_voice_without_fallback(self):
        callback = Mock(side_effect=VoiceAccessError("not accessible"))
        with self.assertRaises(VoiceAccessError):
            check_approved_voices(api_key="test-key", fetch_metadata=callback)
        self.assertEqual(callback.call_count, 1)


if __name__ == "__main__":
    unittest.main()
