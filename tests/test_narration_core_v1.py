import unittest

from app.services.narration_core import (
    NARRATION_CORE_NAMESPACE,
    NARRATION_CORE_VERSION,
    NarrationCoreError,
    build_narration_artifact,
    narration_fingerprint,
    require_current_core,
)


class NarrationCoreV1Tests(unittest.TestCase):
    def test_plain_prose_is_preserved(self):
        artifact = build_narration_artifact(
            "Jesus permanece conosco mesmo nos dias mais difíceis."
        )
        self.assertEqual(
            artifact.spoken_text,
            "Jesus permanece conosco mesmo nos dias mais difíceis.",
        )
        self.assertEqual(artifact.core_version, 1)
        self.assertEqual(artifact.namespace, NARRATION_CORE_NAMESPACE)

    def test_portuguese_production_script_keeps_only_narration(self):
        raw = """CENA 1
NARRAÇÃO: Jesus permanece conosco mesmo nos dias mais difíceis.

PROMPT VISUAL: Jesus caminhando por uma estrada, iluminação cinematográfica, 16:9.
DURAÇÃO: 8 segundos.
MOVIMENTO DE CÂMERA: travelling lento para frente.
TEXTO NA TELA: Deus não esqueceu de você.
"""
        artifact = build_narration_artifact(raw)
        self.assertEqual(
            artifact.spoken_text,
            "Jesus permanece conosco mesmo nos dias mais difíceis.",
        )
        self.assertGreaterEqual(artifact.removed_technical_blocks, 5)

    def test_scene_and_narration_on_same_line(self):
        artifact = build_narration_artifact(
            "CENA 2 — NARRAÇÃO: A esperança renasce quando confiamos em Deus."
        )
        self.assertEqual(
            artifact.spoken_text,
            "A esperança renasce quando confiamos em Deus.",
        )

    def test_json_extracts_only_narrative_fields(self):
        artifact = build_narration_artifact(
            {
                "scene": 1,
                "narration": "Deus continua escrevendo a sua história.",
                "image_prompt": "cinematic portrait, volumetric light",
                "duration": 8,
                "camera_movement": "slow push in",
            }
        )
        self.assertEqual(
            artifact.spoken_text,
            "Deus continua escrevendo a sua história.",
        )
        self.assertNotIn("cinematic", artifact.spoken_text)

    def test_nested_scenes_are_flattened_as_speech_only(self):
        payload = {
            "scenes": [
                {
                    "narration_text": "Primeira frase segura.",
                    "visual_prompt": "wide shot",
                },
                {
                    "narration": "Segunda frase segura.",
                    "on_screen_text": "não narrar",
                },
            ]
        }
        artifact = build_narration_artifact(payload)
        self.assertEqual(
            artifact.spoken_text,
            "Primeira frase segura. Segunda frase segura.",
        )

    def test_stage_direction_is_removed(self):
        artifact = build_narration_artifact(
            "NARRAÇÃO: Deus está perto. [pausa dramática] Ele não abandonou você."
        )
        self.assertEqual(
            artifact.spoken_text,
            "Deus está perto. Ele não abandonou você.",
        )

    def test_bible_reference_is_preserved(self):
        artifact = build_narration_artifact(
            "Romanos 8:17 nos lembra que somos herdeiros com Cristo."
        )
        self.assertIn("Romanos 8:17", artifact.spoken_text)

    def test_pure_technical_payload_is_blocked(self):
        with self.assertRaises(NarrationCoreError):
            build_narration_artifact(
                {
                    "image_prompt": "cinematic Jesus",
                    "duration": 8,
                    "camera_movement": "pan left",
                }
            )

    def test_old_core_metadata_is_rejected(self):
        with self.assertRaises(NarrationCoreError):
            require_current_core(
                {
                    "narration_core_version": 0,
                    "narration_core_namespace": "legacy",
                }
            )

    def test_current_core_metadata_is_accepted(self):
        require_current_core(
            {
                "narration_core_version": NARRATION_CORE_VERSION,
                "narration_core_namespace": NARRATION_CORE_NAMESPACE,
            }
        )

    def test_fingerprint_changes_by_voice_and_provider(self):
        text = "Jesus é a nossa esperança."
        a = narration_fingerprint(spoken_text=text, voice="A", provider="edge_tts")
        b = narration_fingerprint(spoken_text=text, voice="B", provider="edge_tts")
        c = narration_fingerprint(spoken_text=text, voice="A", provider="openai_tts")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
