import json
import unittest
from pathlib import Path

from app.services.spoken_text_boundary import (
    SPOKEN_TEXT_BOUNDARY_VERSION,
    prepare_spoken_narration_text,
)
from app.services.narration_contract_guard import (
    NarrationContractError,
    install_narration_contract_guard,
    validate_narration_text,
)


class _BoundaryDummyVideoGenerator:
    def __init__(self):
        self.provider_calls = 0
        self.provider_texts = []

    def generate_audio(self, text, *args, **kwargs):
        self.provider_calls += 1
        self.provider_texts.append(text)
        return "/tmp/nonexistent-boundary-v4.mp3"


install_narration_contract_guard(_BoundaryDummyVideoGenerator)


class SpokenTextBoundaryV4Tests(unittest.TestCase):
    def test_portuguese_screenplay_keeps_only_narration(self):
        raw = """
CENA 1
NARRAÇÃO: Jesus continua presente mesmo quando o caminho parece difícil.
PROMPT VISUAL: homem sozinho numa estrada, iluminação cinematográfica, 16:9.
DURAÇÃO: 8 segundos
MOVIMENTO DE CÂMERA: travelling lento para frente.
TEXTO NA TELA: Deus não esqueceu de você.
"""
        self.assertEqual(
            prepare_spoken_narration_text(raw),
            "Jesus continua presente mesmo quando o caminho parece difícil.",
        )

    def test_scene_prefix_plus_narration_is_supported(self):
        raw = "CENA 2 — NARRAÇÃO: Quando o medo chegar, lembre-se de que Deus permanece fiel."
        self.assertEqual(
            prepare_spoken_narration_text(raw),
            "Quando o medo chegar, lembre-se de que Deus permanece fiel.",
        )

    def test_json_extracts_only_narrative_fields(self):
        raw = json.dumps(
            {
                "scene": 1,
                "narration_text": "Jesus é o caminho, a verdade e a vida.",
                "image_prompt": "cinematic portrait, dramatic light",
                "camera_movement": "slow dolly in",
                "duration_sec": 8,
            },
            ensure_ascii=False,
        )
        self.assertEqual(
            prepare_spoken_narration_text(raw),
            "Jesus é o caminho, a verdade e a vida.",
        )

    def test_strict_validator_still_rejects_raw_json(self):
        raw = json.dumps(
            {
                "narration_text": "Jesus é o caminho, a verdade e a vida.",
                "image_prompt": "cinematic portrait",
            },
            ensure_ascii=False,
        )
        with self.assertRaises(NarrationContractError):
            validate_narration_text(raw)

    def test_nested_scenes_extract_narration_in_order(self):
        raw = json.dumps(
            {
                "scenes": [
                    {"narration": "Deus conhece a sua história.", "visual_prompt": "close-up"},
                    {"narration": "E ainda está escrevendo novos capítulos.", "visual_prompt": "sunrise"},
                ]
            },
            ensure_ascii=False,
        )
        self.assertEqual(
            prepare_spoken_narration_text(raw),
            "Deus conhece a sua história. E ainda está escrevendo novos capítulos.",
        )

    def test_stage_directions_are_not_spoken(self):
        raw = "[pausa dramática] Jesus continua presente. [tom suave] Você não está sozinho."
        self.assertEqual(
            prepare_spoken_narration_text(raw),
            "Jesus continua presente. Você não está sozinho.",
        )

    def test_scripture_reference_is_preserved(self):
        raw = "Somos filhos e herdeiros com Cristo. Romanos 8:17."
        self.assertEqual(prepare_spoken_narration_text(raw), raw)

    def test_technical_only_json_fails_closed(self):
        with self.assertRaises(ValueError):
            prepare_spoken_narration_text('{"image_prompt":"cinematic","duration":8}')

    def test_residual_visual_prompt_fails_closed(self):
        with self.assertRaises(ValueError):
            prepare_spoken_narration_text(
                "Jesus permanece fiel. Depois use prompt visual com câmera cinematográfica."
            )

    def test_guarded_tts_port_sends_only_spoken_text_to_provider(self):
        raw = """
CENA 1
NARRAÇÃO: Cristo é a nossa esperança.
PROMPT DE IMAGEM: luz dourada, cinematic, ultra detailed.
DURAÇÃO: 7 segundos
"""
        generator = _BoundaryDummyVideoGenerator()
        generator.generate_audio(raw)
        self.assertEqual(generator.provider_calls, 1)
        self.assertEqual(generator.provider_texts, ["Cristo é a nossa esperança."])

    def test_ambiguous_technical_payload_never_reaches_tts(self):
        generator = _BoundaryDummyVideoGenerator()
        with self.assertRaises(NarrationContractError):
            generator.generate_audio(
                "PROMPT VISUAL: cinematic portrait\nDURAÇÃO: 8 segundos\nMOVIMENTO DE CÂMERA: zoom lento"
            )
        self.assertEqual(generator.provider_calls, 0)

    def test_contract_version_and_cache_invalidation_are_applied(self):
        self.assertEqual(SPOKEN_TEXT_BOUNDARY_VERSION, 4)
        root = Path(__file__).resolve().parents[1]
        yt_gate = (root / "app/services/youtube_narration_gate.py").read_text(encoding="utf-8")
        lab = (root / "app/services/narration_lab.py").read_text(encoding="utf-8")
        gate_js = (root / "app/static/youtube_narration_gate.js").read_text(encoding="utf-8")
        router = (root / "app/routers/youtube.py").read_text(encoding="utf-8")
        guard = (root / "app/services/narration_contract_guard.py").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_SPOKEN_TEXT_BOUNDARY_V4", guard)
        self.assertIn("NARRATION_GATE_CONTRACT_VERSION = 4", yt_gate)
        self.assertIn('"contract_version": 4', lab)
        self.assertIn("approvedNarration.v4", gate_js)
        self.assertIn('int(approved_meta.get("narration_contract_version") or 0) >= 4', router)


if __name__ == "__main__":
    unittest.main()
