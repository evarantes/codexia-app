from unittest.mock import patch

from app.services.video_generator import DIRECTOR_ACCEPTED_CAPTION_TIMELINE_SOURCES, VideoGenerator


class MissingTranscriptionService:
    def transcribe_audio_segments_detailed(self, _audio_path, language="pt"):
        return {"segments": [], "error": "TRANSCRIPTION_PROVIDER_MISSING", "language": language}


def test_missing_transcription_provider_uses_measured_audio_text_timeline(tmp_path):
    generator = VideoGenerator(output_dir=str(tmp_path), ai_service=MissingTranscriptionService())
    narration = "E se Deus não existisse? Imagine a vida sem esperança."

    with patch.object(
        generator,
        "_caption_timeline_from_audio_activity",
        return_value={"timeline": [], "error": "audio_activity_boundaries_unavailable"},
    ):
        result = generator._build_caption_timeline_details(
            narration,
            duration=12.0,
            audio_path=str(tmp_path / "final-narration.mp3"),
        )

    timeline = result["timeline"]
    assert timeline
    assert result["source"] == "text_fallback_from_measured_audio"
    assert result["source"] in DIRECTOR_ACCEPTED_CAPTION_TIMELINE_SOURCES
    assert result["timing_source"] == "measured_audio_duration"
    assert result["alignment_quality"] == "estimated_from_measured_audio_duration"
    assert "".join(item["caption"] for item in timeline).replace(" ", "") == narration.replace(" ", "")
    assert timeline[-1]["end"] == 12.0
