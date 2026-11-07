import unittest
from unittest.mock import Mock
from app.services.entre_nos_dois_voice_check import run_voice_check
from app.services.entre_nos_dois_voice_preflight import VoiceAccessError


class VoiceCheckTests(unittest.TestCase):
    def test_missing_key_never_calls_provider(self):
        checker = Mock()
        result = run_voice_check(provider_settings={"elevenlabs_api_key": {"value": None}}, checker=checker)
        self.assertEqual(result["status"], "configuration_required")
        checker.assert_not_called()

    def test_verified_voices_are_ready_for_cost_approval(self):
        checker = Mock(return_value={
            "all_accessible": True,
            "checked": [
                {"speaker": "Gabriel", "provider_name": "Andrey", "accessible": True},
                {"speaker": "Helena", "provider_name": "Luciana", "accessible": True},
            ],
        })
        result = run_voice_check(provider_settings={"elevenlabs_api_key": {"value": "fake-secret"}}, checker=checker)
        self.assertEqual(result["status"], "ready_for_cost_approval")
        self.assertNotIn("fake-secret", str(result))
        self.assertFalse(result["billable_generation_performed"])
        checker.assert_called_once_with(api_key="fake-secret")

    def test_unavailable_voice_fails_closed(self):
        checker = Mock(side_effect=VoiceAccessError("Cannot access voice"))
        result = run_voice_check(provider_settings={"elevenlabs_api_key": {"value": "fake-secret"}}, checker=checker)
        self.assertEqual(result["status"], "voice_access_unconfirmed")
        self.assertNotIn("fake-secret", str(result))


if __name__ == "__main__":
    unittest.main()
