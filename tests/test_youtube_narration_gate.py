import sys
import tempfile
import types
import unittest
from pathlib import Path

from app.services.narration_core import NARRATION_CORE_NAMESPACE, NARRATION_CORE_VERSION
from app.services.youtube_narration_gate import (
    YouTubeNarrationGateError,
    YouTubeNarrationGateService,
)


class _FakeCommunicate:
    calls = 0
    texts = []

    def __init__(self, text, voice, **kwargs):
        self.text = text
        self.voice = voice
        type(self).texts.append(text)

    async def save(self, path):
        type(self).calls += 1
        Path(path).write_bytes(b"ID3" + b"a" * 2048)


class YouTubeNarrationGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = YouTubeNarrationGateService(self.tmp.name)
        self.old_edge = sys.modules.get("edge_tts")
        sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=_FakeCommunicate)
        _FakeCommunicate.calls = 0
        _FakeCommunicate.texts = []
        self.service._duration = lambda _path: 123.4

    def tearDown(self):
        self.tmp.cleanup()
        if self.old_edge is None:
            sys.modules.pop("edge_tts", None)
        else:
            sys.modules["edge_tts"] = self.old_edge

    def test_blocks_pure_technical_payload_before_tts(self):
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.generate(
                text='{"image_prompt": "Jesus"}',
                user_id=7,
                voice="auto",
                voice_gender="female",
            )
        self.assertEqual(ctx.exception.code, "NARRATION_CORE_BLOCKED")
        self.assertEqual(_FakeCommunicate.calls, 0)

    def test_mixed_production_script_sends_only_spoken_sentence(self):
        raw = """CENA 1
NARRAÇÃO: Jesus permanece conosco mesmo nos dias mais difíceis.
PROMPT VISUAL: Jesus caminhando por uma estrada, iluminação cinematográfica, 16:9.
DURAÇÃO: 8 segundos.
MOVIMENTO DE CÂMERA: travelling lento.
TEXTO NA TELA: Deus não esqueceu de você.
"""
        result = self.service.generate(text=raw, user_id=7)
        self.assertEqual(_FakeCommunicate.calls, 1)
        self.assertEqual(_FakeCommunicate.texts, ["Jesus permanece conosco mesmo nos dias mais difíceis."])
        self.assertEqual(result["spoken_text_sent_to_tts"], _FakeCommunicate.texts[0])
        self.assertGreaterEqual(result["removed_technical_blocks"], 5)

    def test_generates_once_and_reuses_identical_audio(self):
        text = "Esta é uma narração limpa, completa e pronta para o vídeo."
        first = self.service.generate(text=text, user_id=7, voice="auto", voice_gender="female")
        second = self.service.generate(text=text, user_id=7, voice="auto", voice_gender="female")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["preview_id"], second["preview_id"])
        self.assertEqual(first["narration_core_version"], NARRATION_CORE_VERSION)
        self.assertEqual(first["narration_core_namespace"], NARRATION_CORE_NAMESPACE)
        self.assertEqual(_FakeCommunicate.calls, 1)

    def test_approval_returns_reuse_audio_and_rejects_changed_text(self):
        text = "Confie em Deus e permaneça firme até o fim."
        preview = self.service.generate(text=text, user_id=9)
        approved = self.service.approve(
            preview_id=preview["preview_id"],
            expected_text=text,
            user_id=9,
        )
        self.assertTrue(approved["approved"])
        self.assertEqual(approved["reuse_audio_from"]["source"], "youtube_narration_core_v1_approved")
        self.assertEqual(approved["reuse_audio_from"]["narration_core_version"], NARRATION_CORE_VERSION)
        self.assertTrue(Path(approved["reuse_audio_from"]["output_path"]).is_file())
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.approve(
                preview_id=preview["preview_id"],
                expected_text="Confie em Deus, mas este texto mudou.",
                user_id=9,
            )
        self.assertEqual(ctx.exception.code, "TEXT_CHANGED_AFTER_PREVIEW")

    def test_job_approval_freezes_the_exact_mp3_in_its_own_folder(self):
        text = "Jesus nos chama a caminhar com fé e esperança."
        preview = self.service.generate(text=text, user_id=11, theme="Esperança")
        job_id = preview["production_job_id"]

        approved = self.service.approve(
            preview_id=preview["preview_id"],
            expected_text=text,
            user_id=11,
            production_job_id=job_id,
        )

        approved_path = Path(approved["reuse_audio_from"]["output_path"])
        self.assertEqual(approved["production_job_id"], job_id)
        self.assertEqual(approved["production_job_status"], "narration_approved")
        self.assertEqual(approved_path.name, "approved_narration.mp3")
        self.assertTrue(approved_path.is_file())
        validated = self.service.job_store.validated_approved_audio(user_id=11, job_id=job_id)
        self.assertEqual(validated["audio_path"].resolve(), approved_path.resolve())
        self.assertTrue(validated["job"]["tts_locked"])


if __name__ == "__main__":
    unittest.main()
