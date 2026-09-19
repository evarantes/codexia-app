from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.video_generator import VideoGenerator


def test_local_audio_activity_alignment_uses_real_speech_intervals(tmp_path: Path):
    audio = tmp_path / "narration.mp3"
    audio.write_bytes(b"ID3" + (b"x" * 2000))
    diagnostic = "\n".join([
        "[silencedetect] silence_start: 0",
        "[silencedetect] silence_end: 1.0 | silence_duration: 1.0",
        "[silencedetect] silence_start: 4.0",
        "[silencedetect] silence_end: 5.0 | silence_duration: 1.0",
        "[silencedetect] silence_start: 8.5",
        "[silencedetect] silence_end: 10.0 | silence_duration: 1.5",
    ])
    completed = SimpleNamespace(returncode=0, stderr=diagnostic, stdout="")

    generator = VideoGenerator(output_dir=str(tmp_path), ai_service=None)
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch(
        "subprocess.run", return_value=completed
    ):
        details = generator._caption_timeline_from_audio_activity(
            "Primeira frase da mensagem. Segunda frase para continuar.",
            10.0,
            str(audio),
        )

    assert details["source"] == "local_audio_activity_alignment"
    assert details["timing_source"] == "ffmpeg_silencedetect_real_audio"
    assert details["silence_count"] == 3
    assert details["speech_interval_count"] == 2
    assert details["timeline"]
    assert details["timeline"][0]["start"] >= 1.0
    assert details["timeline"][-1]["end"] <= 8.5


def test_local_alignment_fails_closed_without_audio_boundaries(tmp_path: Path):
    audio = tmp_path / "narration.mp3"
    audio.write_bytes(b"ID3" + (b"x" * 2000))
    completed = SimpleNamespace(returncode=0, stderr="", stdout="")
    generator = VideoGenerator(output_dir=str(tmp_path), ai_service=None)

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch(
        "subprocess.run", return_value=completed
    ):
        details = generator._caption_timeline_from_audio_activity(
            "Uma frase sem pausas detectadas.", 5.0, str(audio)
        )

    assert details["timeline"] == []
    assert details["error"] == "audio_activity_boundaries_unavailable"
