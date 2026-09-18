import unittest

from app.routers.youtube import VideoRequest
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator


class _FailingPremiumAudioService:
    def __init__(self):
        self.last_kwargs = None

    def select_tts_voice_hint(self, *args, **kwargs):
        return "onyx"

    def generate_audio(self, *args, **kwargs):
        return None

    def generate_audio_with_diagnostics(self, text, **kwargs):
        self.last_kwargs = dict(kwargs)
        return {
            "configured_provider": "elevenlabs",
            "provider_used": None,
            "fallback_used": False,
            "attempts": [
                {
                    "provider": "elevenlabs",
                    "status": "failed",
                    "reason": "provider unavailable",
                }
            ],
            "audio_content": None,
            "error_summary": "Nenhum provider premium conseguiu gerar audio.",
        }


class TestV2PremiumVoicePolicy(unittest.TestCase):
    def test_video_request_preserves_premium_voice_contract(self):
        request = VideoRequest(
            topic="Devocional",
            mode="story",
            kind="devotional",
            premium_voice_required=True,
        )
        self.assertTrue(request.premium_voice_required)


    def test_premium_required_blocks_edge_fallback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_path:
            service = _FailingPremiumAudioService()
            generator = VideoGenerator(output_dir=tmp_path, ai_service=service)

            with self.assertRaisesRegex(RuntimeError, "Voz premium obrigatória indisponível"):
                generator.generate_audio(
                    "Uma narração curta para teste.",
                    voice_style="human",
                    voice_gender="female",
                    premium_voice_required=True,
                )

            self.assertIsNotNone(service.last_kwargs)
            self.assertFalse(service.last_kwargs["allow_provider_fallback"])
            self.assertTrue(generator._last_tts_debug["fallback_blocked"])
            self.assertIsNone(generator._last_tts_debug["provider_used"])

    def test_caption_transcription_uses_task_context(self):
        import tempfile
        from pathlib import Path
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        audio = Path(temp_dir.name) / "narration.mp3"
        audio.write_bytes(b"fake-audio")

        captured = {}

        class _Router:
            def transcribe_audio(self, **kwargs):
                captured.update(kwargs)
                return {
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": "Olá mundo",
                            "words": [
                                {"start": 0.0, "end": 0.4, "word": "Olá"},
                                {"start": 0.4, "end": 1.0, "word": "mundo"},
                            ],
                        }
                    ],
                    "error": None,
                }

        generator = AIContentGenerator.__new__(AIContentGenerator)
        generator._load_config = lambda: None
        generator.ai_router = _Router()
        generator.ai_user_id = 42
        generator.ai_task_id = "task-v2"
        generator.ai_video_id = "video-v2"

        result = generator.transcribe_audio_segments_detailed(str(audio), language="pt")

        self.assertIsNone(result["error"])
        self.assertTrue(result["segments"])
        self.assertEqual(captured["user_id"], 42)
        self.assertEqual(captured["task_id"], "task-v2")
        self.assertEqual(captured["video_id"], "video-v2")
