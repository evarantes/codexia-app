import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

from app.services.youtube_narration_gate import (
    YouTubeNarrationGateError,
    YouTubeNarrationGateService,
)


class _FakeCommunicate:
    calls = 0

    def __init__(self, text, voice, **kwargs):
        self.text = text
        self.voice = voice

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
        self.service._duration = lambda _path: 123.4

    def tearDown(self):
        self.tmp.cleanup()
        if self.old_edge is None:
            sys.modules.pop("edge_tts", None)
        else:
            sys.modules["edge_tts"] = self.old_edge

    def test_blocks_code_before_tts(self):
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.generate(
                text='{"image_prompt": "Jesus"}',
                user_id=7,
                voice="auto",
                voice_gender="female",
            )
        self.assertEqual(ctx.exception.code, "NARRATION_CONTRACT_BLOCKED")
        self.assertEqual(_FakeCommunicate.calls, 0)

    def test_generates_once_and_reuses_identical_audio(self):
        text = "Esta é uma narração limpa, completa e pronta para o vídeo."
        first = self.service.generate(text=text, user_id=7, voice="auto", voice_gender="female")
        second = self.service.generate(text=text, user_id=7, voice="auto", voice_gender="female")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["preview_id"], second["preview_id"])
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
        self.assertEqual(approved["reuse_audio_from"]["source"], "youtube_narration_gate_approved")
        self.assertTrue(Path(approved["reuse_audio_from"]["output_path"]).is_file())
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.approve(
                preview_id=preview["preview_id"],
                expected_text="Confie em Deus, mas este texto mudou.",
                user_id=9,
            )
        self.assertEqual(ctx.exception.code, "TEXT_CHANGED_AFTER_PREVIEW")


if __name__ == "__main__":
    unittest.main()
