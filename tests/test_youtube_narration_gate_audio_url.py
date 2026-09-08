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
from app.services.youtube_narration_gate import EDGE_TTS_PROVIDER, YouTubeNarrationGateService
from app.services.narrative_structure_standard import (
    audit_canonical_narration,
    compose_canonical_narration,
)


class YouTubeNarrationGateAudioUrlTests(unittest.TestCase):
    def test_generate_returns_registered_protected_audio_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = YouTubeNarrationGateService(output_root=tmp)
            user_dir = service._user_dir(1)
            source_text = compose_canonical_narration({
                "hook": "Uma pergunta relevante abre esta narração segura para teste.",
                "development": "O desenvolvimento apresenta o contexto necessário com clareza.",
                "central_truth": "A verdade central sustenta toda a mensagem cristã.",
                "transformation": "A transformação mostra uma mudança real de perspectiva.",
                "application": "A aplicação conduz a uma escolha concreta para hoje.",
                "climax": "O clímax entrega a ideia principal com força e equilíbrio.",
                "reflection": "A reflexão retoma o início e encerra o raciocínio por completo.",
            })
            artifact = build_narration_artifact(source_text)
            spoken = artifact.spoken_text
            voice = "pt-BR-FranciscaNeural"
            preview_id = narration_fingerprint(
                spoken_text=spoken,
                voice=voice,
                provider=EDGE_TTS_PROVIDER,
            )
            (user_dir / f"{preview_id}.mp3").write_bytes(b"x" * 1024)
            (user_dir / f"{preview_id}.json").write_text(
                json.dumps(
                    {
                        "preview_id": preview_id,
                        "text_sha256": artifact.text_sha256,
                        "voice": voice,
                        "provider": EDGE_TTS_PROVIDER,
                        "approved": False,
                        "narration_core_version": NARRATION_CORE_VERSION,
                        "narration_core_namespace": NARRATION_CORE_NAMESPACE,
                        "spoken_text_sent_to_tts": spoken,
                        "review_script_text": source_text,
                        "narration_contract": audit_canonical_narration(source_text),
                        "caption_timeline": [],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(service, "_duration", return_value=1.0):
                result = service.generate(text=source_text, user_id=1, voice=voice, voice_gender="female")
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
