from __future__ import annotations

import tempfile
import unittest

from app.services.narration_contract_guard import (
    NarrationContractError,
    install_narration_contract_guard,
    validate_narration_text,
)
from app.services.narration_core import build_narration_artifact
from app.services.narration_lab import NarrationLabError, NarrationLabService


class _DummyVideoGenerator:
    def __init__(self):
        self.provider_calls = 0
        self.provider_texts = []

    def generate_audio(self, text, *args, **kwargs):
        self.provider_calls += 1
        self.provider_texts.append(text)
        return "/tmp/dummy.mp3"


install_narration_contract_guard(_DummyVideoGenerator)


class GlobalNarrationContractTests(unittest.TestCase):
    def test_plain_narration_is_accepted(self):
        text = "Jesus permanece conosco mesmo nos dias difíceis. Continue firme na esperança."
        self.assertEqual(validate_narration_text(text), text)

    def test_core_extracts_speech_but_never_visual_prompt(self):
        artifact = build_narration_artifact(
            "CENA 1\nNARRAÇÃO: Jesus é a nossa esperança.\nPROMPT VISUAL: cinematic Jesus, 16:9."
        )
        self.assertEqual(artifact.spoken_text, "Jesus é a nossa esperança.")

    def test_renderer_tts_port_receives_only_core_spoken_text(self):
        video = _DummyVideoGenerator()
        video.generate_audio(
            "CENA 1\nNARRAÇÃO: Cristo permanece fiel.\nPROMPT DE IMAGEM: luz dourada cinematográfica."
        )
        self.assertEqual(video.provider_calls, 1)
        self.assertEqual(video.provider_texts, ["Cristo permanece fiel."])

    def test_serialized_technical_payload_is_blocked(self):
        bad = "status: processing\nprogress: 89\noutput_path: /data/videos/final.mp4"
        with self.assertRaises(NarrationContractError):
            validate_narration_text(bad, require_terminal_sentence=False)

    def test_narration_lab_blocks_technical_text_before_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab = NarrationLabService(output_root=tmp)
            with self.assertRaises(NarrationLabError) as ctx:
                lab.generate(
                    {
                        "provider": "edge_tts",
                        "voice_style": "human",
                        "voice_gender": "female",
                        "text": "PROMPT VISUAL: cinematic portrait. DURAÇÃO: 8 segundos. MOVIMENTO DE CÂMERA: zoom lento.",
                    },
                    user_id=1,
                )
            self.assertEqual(ctx.exception.code, "NARRATION_CONTRACT_BLOCKED")


if __name__ == "__main__":
    unittest.main()
