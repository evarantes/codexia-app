import tempfile
import unittest
from unittest.mock import patch

from app.services.youtube_narration_gate import YouTubeNarrationGateService


class YouTubeNarrationGateAudioUrlTests(unittest.TestCase):
    def test_generate_returns_registered_protected_audio_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = YouTubeNarrationGateService(output_root=tmp)
            user_dir = service._user_dir(1)
            spoken = "Esta é uma narração completa e segura para teste."
            voice = "pt-BR-FranciscaNeural"
            import hashlib
            preview_id = hashlib.sha256(f"v1\n{voice}\n{spoken}".encode("utf-8")).hexdigest()[:32]
            (user_dir / f"{preview_id}.mp3").write_bytes(b"x" * 1024)
            with patch.object(service, "_duration", return_value=1.0):
                result = service.generate(text=spoken, user_id=1, voice=voice, voice_gender="female")
            self.assertEqual(
                result["audio_url"],
                f"/youtube/narration-lab/production-preview/audio/{preview_id}",
            )


if __name__ == "__main__":
    unittest.main()
