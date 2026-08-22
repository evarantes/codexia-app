import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("ENABLE_SQLITE_DEV", "true")
os.environ.setdefault("APP_ENV", "development")
if os.environ.get("DATABASE_URL", "").startswith("postgresql://"):
    del os.environ["DATABASE_URL"]

from app.services.ai_generator import AIContentGenerator
from app.services.task_manager import _mark_row_runtime_paused
from app.services.video_generator import VideoGenerator, _narration_activity_pulse


class _FakePremiumAI:
    def __init__(self):
        self.calls = 0

    def generate_audio(self, *_args, **_kwargs):
        return None

    def generate_audio_with_diagnostics(self, *_args, activity_callback=None, **_kwargs):
        self.calls += 1
        if activity_callback:
            activity_callback("provedor simulado")
        return {
            "provider_used": "openai_tts",
            "fallback_used": False,
            "attempts": [{"provider": "openai_tts", "status": "success"}],
            "audio_content": b"fake-audio" * 200,
        }


class NarrationTimeoutHeartbeatTests(unittest.TestCase):
    def test_activity_pulse_keeps_emitting_during_blocking_provider(self):
        signals = []
        with _narration_activity_pulse(
            signals.append,
            "aguardando provedor",
            interval_seconds=0.02,
        ):
            time.sleep(0.075)

        self.assertGreaterEqual(len(signals), 3)
        self.assertTrue(all(item == "aguardando provedor" for item in signals))

    def test_identical_narration_reuses_preserved_audio_without_second_provider_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_ai = _FakePremiumAI()
            generator = VideoGenerator(output_dir=temp_dir, ai_service=fake_ai)
            with patch.object(generator, "_is_ffprobe_available", return_value=True), \
                 patch.object(generator, "_ffprobe_duration_seconds", return_value=12.0):
                first = generator.generate_audio("Texto idêntico e completo para narração.")
                second = generator.generate_audio("Texto idêntico e completo para narração.")

            self.assertEqual(first, second)
            self.assertTrue(os.path.basename(first).startswith("tts_cache_"))
            self.assertEqual(fake_ai.calls, 1)
            self.assertEqual(generator._last_tts_debug["provider_used"], "preserved_tts_cache")
            self.assertTrue(generator._last_tts_debug["cache_hit"])

    def test_pause_signal_stops_before_any_voice_provider_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_ai = _FakePremiumAI()
            generator = VideoGenerator(output_dir=temp_dir, ai_service=fake_ai)

            def _paused(_message):
                raise RuntimeError("pause-confirmed")

            with self.assertRaisesRegex(RuntimeError, "pause-confirmed"):
                generator.generate_audio(
                    "Texto que não deve chegar ao provedor.",
                    status_callback=_paused,
                )

            self.assertEqual(fake_ai.calls, 0)

    def test_production_task_attempts_only_one_paid_voice_provider(self):
        generator = AIContentGenerator()
        generator.ai_task_id = "task-cost-guard"
        generator.voice_provider = "elevenlabs"
        generator.elevenlabs_key = "test-elevenlabs"
        generator.api_key = "test-openai"
        calls = []

        def _elevenlabs(*_args, **kwargs):
            calls.append(("elevenlabs", kwargs.get("timeout_seconds")))
            return None

        def _openai(*_args, **kwargs):
            calls.append(("openai_tts", kwargs.get("timeout_seconds")))
            return b"should-not-run"

        with patch.object(generator, "_load_config", return_value=None), \
             patch.object(generator, "_is_production_voice_mode", return_value=True), \
             patch.object(generator, "_paid_ai_disabled", return_value=False), \
             patch.object(generator, "_generate_audio_elevenlabs", side_effect=_elevenlabs), \
             patch.object(generator, "_generate_audio_openai_tts", side_effect=_openai), \
             patch.dict(os.environ, {"NARRATION_PROVIDER_TIMEOUT_SECONDS": "37"}, clear=False):
            diagnostics = generator.generate_audio_with_diagnostics("texto seguro")

        self.assertEqual(calls, [("elevenlabs", 37)])
        self.assertEqual(diagnostics["paid_attempts"], 1)
        self.assertEqual(diagnostics["paid_attempt_limit"], 1)
        self.assertTrue(any(
            item.get("provider") == "openai_tts" and item.get("status") == "skipped"
            for item in diagnostics["attempts"]
        ))

    def test_confirmed_pause_refreshes_persisted_runtime_state(self):
        row = SimpleNamespace(result_json=json.dumps({
            "pipeline_stage": "stage_2_voice",
            "runtime_telemetry": {"stage": "stage_2_voice", "heartbeat_at": "old"},
        }))

        _mark_row_runtime_paused(row, "Produção pausada com segurança.")

        result = json.loads(row.result_json)
        self.assertEqual(result["pipeline_stage"], "stage_2_voice")
        self.assertEqual(result["runtime_telemetry"]["stage"], "paused")
        self.assertEqual(result["runtime_telemetry"]["state"], "paused")
        self.assertNotEqual(result["runtime_telemetry"]["heartbeat_at"], "old")


if __name__ == "__main__":
    unittest.main()
