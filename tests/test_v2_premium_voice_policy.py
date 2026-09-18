import pytest

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


def test_video_request_preserves_premium_voice_contract():
    request = VideoRequest(
        topic="Devocional",
        mode="story",
        kind="devotional",
        premium_voice_required=True,
    )
    assert request.premium_voice_required is True


def test_premium_required_blocks_edge_fallback(tmp_path):
    service = _FailingPremiumAudioService()
    generator = VideoGenerator(output_dir=str(tmp_path), ai_service=service)

    with pytest.raises(RuntimeError, match="Voz premium obrigatória indisponível"):
        generator.generate_audio(
            "Uma narração curta para teste.",
            voice_style="human",
            voice_gender="female",
            premium_voice_required=True,
        )

    assert service.last_kwargs is not None
    assert service.last_kwargs["allow_provider_fallback"] is False
    assert generator._last_tts_debug["fallback_blocked"] is True
    assert generator._last_tts_debug["provider_used"] is None


def test_caption_transcription_uses_task_context(tmp_path):
    audio = tmp_path / "narration.mp3"
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

    assert result["error"] is None
    assert result["segments"]
    assert captured["user_id"] == 42
    assert captured["task_id"] == "task-v2"
    assert captured["video_id"] == "video-v2"
