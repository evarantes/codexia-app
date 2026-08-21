import os
import tempfile
import unittest
from pathlib import Path

from app.services.narration_contract_guard import (
    DEFAULT_COMPLETE_CTA,
    NarrationContractError,
    has_complete_cta,
    install_narration_contract_guard,
    validate_narration_text,
)


class DummyVideoGenerator:
    def __init__(self):
        self.provider_calls = 0

    def _count_words(self, text):
        return len(str(text or "").split())

    def _estimate_text_duration_with_voice(self, text, **_kwargs):
        return len(str(text or "").split()) / 2.4

    def prepare_final_narration_text(self, plan, scenes, voice_style=None, voice_gender=None):
        story = " ".join(str(scene.get("text") or "") for scene in scenes)
        reflection = "Cristo continua sendo nossa esperança."
        return {
            "opening_text": "Hoje existe uma resposta para você.",
            "body_text": f"{story} {reflection}".strip(),
            "reflection_text": reflection,
            "cta_text": "Inscreva-se no canal.",
            "closing_text": "Inscreva-se no canal.",
            "full_text": story,
            "intro_opening_hold_sec": 0.4,
            "opening_duration_est_sec": 2.0,
            "body_duration_est_sec": 4.0,
            "pause_duration_sec": 0.5,
        }

    def generate_audio(self, text, *args, **kwargs):
        self.provider_calls += 1
        return "/tmp/nonexistent.mp3"

    def _compose_segmented_narration_audio(self, *, main_text, cta_text, **kwargs):
        return {"main_text": main_text, "cta_text": cta_text}


class NarrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_narration_contract_guard(DummyVideoGenerator)

    def test_json_or_internal_code_never_reaches_tts_provider(self):
        generator = DummyVideoGenerator()
        with self.assertRaises(NarrationContractError):
            generator.generate_audio('Jesus é o caminho. {"image_prompt": "cinematic"}')
        self.assertEqual(generator.provider_calls, 0)

    def test_truncated_sentence_is_blocked_before_tts(self):
        with self.assertRaises(NarrationContractError):
            validate_narration_text("Ele é o Rei que você")

    def test_normal_narration_remains_valid(self):
        self.assertEqual(
            validate_narration_text("Jesus é o caminho, a verdade e a vida."),
            "Jesus é o caminho, a verdade e a vida.",
        )

    def test_default_cta_has_subscribe_bell_and_share(self):
        self.assertTrue(has_complete_cta(DEFAULT_COMPLETE_CTA))

    def test_legacy_cta_scene_is_removed_from_condensable_story_and_protected(self):
        generator = DummyVideoGenerator()
        scenes = [
            {"text": "Jesus transforma vidas."},
            {
                "text": "Inscreva-se, ative o sininho e compartilhe este vídeo.",
                "codexia_narrated_channel_cta": True,
            },
        ]
        meta = generator.prepare_final_narration_text({}, scenes)
        self.assertEqual(len(scenes), 1)
        self.assertNotIn("Inscreva-se", meta["body_text"])
        self.assertIn("Cristo continua sendo nossa esperança.", meta["full_text"])
        self.assertTrue(has_complete_cta(meta["cta_text"]))
        self.assertTrue(meta["full_text"].endswith(meta["cta_text"]))

    def test_incomplete_cta_is_repaired_before_segmented_tts(self):
        generator = DummyVideoGenerator()
        result = generator._compose_segmented_narration_audio(
            main_text="Jesus nos chama para perto.",
            cta_text="Inscreva-se no canal.",
        )
        self.assertTrue(has_complete_cta(result["cta_text"]))

    def test_immediate_manifest_copy_survives_source_deletion(self):
        # apply_narration_contract_hardening.py runs before the full test suite in CI.
        from app.services.production_manifest import record_artifact

        with tempfile.TemporaryDirectory() as manifest_root, tempfile.TemporaryDirectory() as source_root:
            prior = os.environ.get("CODEXIA_PRODUCTION_MANIFEST_DIR")
            os.environ["CODEXIA_PRODUCTION_MANIFEST_DIR"] = manifest_root
            try:
                source = Path(source_root) / "temp_scene.png"
                source.write_bytes(b"x" * 4096)
                entry = record_artifact("task-contract-test", str(source), kind="image", source="unit_test")
                durable = Path(str(entry.get("durable_path") or ""))
                self.assertTrue(durable.is_file())
                source.unlink()
                self.assertTrue(durable.is_file(), "durable manifest asset must survive temp cleanup")
                self.assertEqual(durable.read_bytes(), b"x" * 4096)
            finally:
                if prior is None:
                    os.environ.pop("CODEXIA_PRODUCTION_MANIFEST_DIR", None)
                else:
                    os.environ["CODEXIA_PRODUCTION_MANIFEST_DIR"] = prior

    def test_hardening_markers_are_present_after_ci_apply(self):
        root = Path(__file__).resolve().parents[1]
        video = (root / "app/services/video_generator.py").read_text(encoding="utf-8")
        youtube = (root / "app/routers/youtube.py").read_text(encoding="utf-8")
        manifest = (root / "app/services/production_manifest.py").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_NARRATION_CONTRACT_PROTECTED_CLOSING_V1", video)
        self.assertIn("CODEXIA_POST_RENDER_DURATION_GATE_V1", video)
        self.assertIn("CODEXIA_NARRATION_CONTRACT_RUNTIME_V1", youtube)
        self.assertIn("CODEXIA_IMMEDIATE_ARTIFACT_MANIFEST_V1", manifest)
        self.assertIn("audio_duration * 0.015", manifest)


if __name__ == "__main__":
    unittest.main()
