import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ENABLE_SQLITE_DEV", "true")

from app.services.ai_generator import AIContentGenerator
from app.services.narration_lab import NarrationLabError, NarrationLabService


class _FakeAI:
    calls = []

    def __init__(self):
        self.api_key = "test-openai-key"
        self.elevenlabs_key = "test-eleven-key"
        self.elevenlabs_voice_id = "customVoice123"
        self.elevenlabs_voice_name = "Voz Teste"

    def _load_config(self):
        return None

    def _paid_ai_disabled(self):
        return False

    def _automatic_voice_hint(self, voice_style=None, voice_gender=None, preferred_provider=None):
        return "onyx" if voice_gender == "male" else "nova"

    def _resolve_elevenlabs_voice_selection(self, voice_hint):
        return {
            "requested_voice_hint": voice_hint,
            "effective_voice_hint": voice_hint,
            "voice_id_used": "customVoice123" if voice_hint == "my_voice" else "providerVoice123",
            "voice_name_used": "Voz Teste" if voice_hint == "my_voice" else voice_hint,
            "voice_selection_source": "fake",
        }

    def generate_audio_with_diagnostics(self, text, **kwargs):
        self.__class__.calls.append({"text": text, **kwargs})
        return {
            "provider_used": kwargs.get("preferred_provider"),
            "fallback_used": False,
            "attempts": [{"provider": kwargs.get("preferred_provider"), "status": "success"}],
            "audio_content": b"fake-mp3" * 400,
        }


class NarrationLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NarrationLabService(output_root=self.temp.name)
        self.service._probe_audio_file = lambda path: {
            "ok": Path(path).is_file(),
            "audio_duration_sec": 8.4,
            "audio_size_bytes": Path(path).stat().st_size if Path(path).is_file() else 0,
            "error": None,
        }
        _FakeAI.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def _payload(self, **overrides):
        payload = {
            "text": "Deus permanece presente em todos os momentos. Confie e siga em frente.",
            "provider": "openai_tts",
            "voice": "auto",
            "voice_style": "human",
            "voice_gender": "female",
            "confirm_paid_generation": True,
        }
        payload.update(overrides)
        return payload

    @patch("app.services.narration_lab.AIContentGenerator", _FakeAI)
    def test_paid_sample_uses_exact_provider_and_never_renders_video(self):
        result = self.service.generate(self._payload(), user_id=7)

        self.assertEqual(result["provider_used"], "openai_tts")
        self.assertFalse(result["fallback_used"])
        self.assertFalse(result["rendered_video"])
        self.assertFalse(result["queued_video_task"])
        self.assertEqual(result["generated_images"], 0)
        self.assertEqual(result["generated_mp4_count"], 0)
        self.assertTrue(result["charged_new_generation"])
        self.assertEqual(len(_FakeAI.calls), 1)
        self.assertFalse(_FakeAI.calls[0]["allow_provider_fallback"])
        self.assertEqual(_FakeAI.calls[0]["preferred_provider"], "openai_tts")
        self.assertEqual(_FakeAI.calls[0]["text"], result["spoken_text_sent_to_tts"])

    @patch("app.services.narration_lab.AIContentGenerator", _FakeAI)
    def test_identical_sample_reuses_cache_without_second_paid_call(self):
        first = self.service.generate(self._payload(), user_id=8)
        second = self.service.generate(self._payload(), user_id=8)

        self.assertEqual(first["sample_id"], second["sample_id"])
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["charged_new_generation"])
        self.assertEqual(len(_FakeAI.calls), 1)

    @patch("app.services.narration_lab.AIContentGenerator", _FakeAI)
    def test_orphaned_preserved_audio_recovers_metadata_without_paid_call(self):
        first = self.service.generate(self._payload(), user_id=81)
        metadata_path = self.service._user_dir(81) / f"{first['sample_id']}.json"
        metadata_path.unlink()
        _FakeAI.calls = []

        recovered = self.service.generate(self._payload(), user_id=81)

        self.assertTrue(recovered["cache_hit"])
        self.assertFalse(recovered["charged_new_generation"])
        self.assertEqual(_FakeAI.calls, [])
        self.assertTrue(metadata_path.is_file())

    @patch("app.services.narration_lab.AIContentGenerator", _FakeAI)
    def test_paid_confirmation_blocks_before_provider_initialization(self):
        with self.assertRaises(NarrationLabError) as caught:
            self.service.generate(
                self._payload(confirm_paid_generation=False),
                user_id=9,
            )
        self.assertEqual(caught.exception.code, "PAID_CONFIRMATION_REQUIRED")
        self.assertEqual(_FakeAI.calls, [])

    @patch("app.services.narration_lab.AIContentGenerator", _FakeAI)
    def test_structural_code_is_blocked_before_paid_call(self):
        with self.assertRaises(NarrationLabError) as caught:
            self.service.generate(
                self._payload(text='{"image_prompt": "não narrar"}.'),
                user_id=10,
            )
        self.assertEqual(caught.exception.code, "NARRATION_CONTRACT_BLOCKED")
        self.assertEqual(_FakeAI.calls, [])

    def test_edge_sample_is_audio_only_and_free(self):
        def fake_edge(_text, output_path, _voice, _style, _gender):
            output_path.write_bytes(b"edge-audio" * 400)

        self.service._generate_edge_audio = fake_edge
        result = self.service.generate(
            self._payload(provider="edge_tts", confirm_paid_generation=False),
            user_id=11,
        )

        self.assertEqual(result["provider_used"], "edge_tts")
        self.assertFalse(result["paid_provider"])
        self.assertFalse(result["charged_new_generation"])
        self.assertFalse(result["rendered_video"])

    @patch.object(AIContentGenerator, "_load_config", lambda self: None)
    @patch.object(AIContentGenerator, "_is_production_voice_mode", lambda self: False)
    @patch.object(AIContentGenerator, "_paid_ai_disabled", lambda self: False)
    def test_ai_diagnostics_disables_cross_provider_fallback(self):
        generator = AIContentGenerator()
        generator.voice_provider = "elevenlabs"
        generator.elevenlabs_key = "key-eleven"
        generator.api_key = "key-openai"
        generator.edenai_key = None
        generator.elevenlabs_voice_id = "customVoice123"
        generator.elevenlabs_voice_name = "Teste"

        with patch.object(generator, "_generate_audio_elevenlabs", return_value=None) as eleven, \
             patch.object(generator, "_generate_audio_openai_tts", return_value=b"should-not-run") as openai:
            diagnostics = generator.generate_audio_with_diagnostics(
                "Texto seguro para testar o provedor escolhido.",
                preferred_provider="elevenlabs",
                allow_provider_fallback=False,
            )

        eleven.assert_called_once()
        openai.assert_not_called()
        self.assertIsNone(diagnostics["provider_used"])
        self.assertFalse(diagnostics["provider_fallback_allowed"])
        self.assertEqual([item["provider"] for item in diagnostics["attempts"]], ["elevenlabs"])


if __name__ == "__main__":
    unittest.main()
