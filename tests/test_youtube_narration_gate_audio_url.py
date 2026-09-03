import json
import tempfile
import unittest
from unittest.mock import patch

from app.services.narration_core import (
    NARRATION_CORE_NAMESPACE,
    NARRATION_CORE_VERSION,
    build_narration_artifact,
    narration_fingerprint,
)
from app.services.youtube_narration_gate import YouTubeNarrationGateService


class YouTubeNarrationGateAudioUrlTests(unittest.TestCase):
    def test_generate_returns_registered_protected_audio_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = YouTubeNarrationGateService(output_root=tmp)
            user_dir = service._user_dir(1)
            spoken = "Esta é uma narração completa e segura para teste."
            voice = "pt-BR-FranciscaNeural"
            artifact = build_narration_artifact(spoken)
            preview_id = narration_fingerprint(
                spoken_text=spoken,
                voice=voice,
                provider="edge_tts",
            )
            (user_dir / f"{preview_id}.mp3").write_bytes(b"x" * 1024)
            (user_dir / f"{preview_id}.json").write_text(
                json.dumps(
                    {
                        "preview_id": preview_id,
                        "text_sha256": artifact.text_sha256,
                        "voice": voice,
                        "provider": "edge_tts",
                        "approved": False,
                        "narration_core_version": NARRATION_CORE_VERSION,
                        "narration_core_namespace": NARRATION_CORE_NAMESPACE,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(service, "_duration", return_value=1.0):
                result = service.generate(text=spoken, user_id=1, voice=voice, voice_gender="female")
            self.assertEqual(
                result["audio_url"],
                f"/youtube/narration-lab/production-preview/audio/{preview_id}",
            )
            self.assertEqual(result["preview_id"], preview_id)
            self.assertEqual(result["narration_core_version"], NARRATION_CORE_VERSION)
            self.assertEqual(result["narration_core_namespace"], NARRATION_CORE_NAMESPACE)
            self.assertTrue(result["cache_hit"])


if __name__ == "__main__":
    unittest.main()
