import hashlib
import os
import tempfile
import unittest
from unittest import mock

from app.services import audio_checkpoint as ac


class _FakeAI:
    def __init__(self, task_id="task-audio-checkpoint"):
        self.ai_task_id = task_id


class AudioCheckpointRegressionTests(unittest.TestCase):
    def _audio_file(self, payload=b"fake-mp3-content" * 200):
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(payload)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _fake_generator_class(self, audio_path, *, fail_after_audio=False):
        class FakeGenerator:
            def __init__(self):
                self.ai_service = _FakeAI()
                self._last_tts_debug = {
                    "configured_provider": "elevenlabs",
                    "provider_used": "edge_tts",
                    "fallback_used": True,
                    "requested_voice_hint": "nova",
                    "requested_voice_style": "human",
                    "requested_voice_gender": "female",
                    "attempts": [
                        {"provider": "elevenlabs", "status": "failed", "reason": "quota unavailable"},
                        {"provider": "edge_tts", "status": "success"},
                    ],
                }

            def _compose_segmented_narration_audio(self, *, main_text, cta_text, **_kwargs):
                return {
                    "audio_path": audio_path,
                    "main_audio_path": audio_path,
                    "cta_audio_path": None,
                    "main_duration_sec": 12.5,
                    "cta_duration_sec": 0.0,
                    "total_duration_sec": 12.5,
                    "initial_silence_duration_sec": 0.45,
                    "pause_duration_sec": 1.25,
                }

            def create_video_from_plan(self, plan, *args, **kwargs):
                self._compose_segmented_narration_audio(
                    main_text="Texto final aprovado para a narração.",
                    cta_text="Inscreva-se no canal.",
                )
                if fail_after_audio:
                    raise RuntimeError(
                        "Falha de validação da IA Crítica de Áudio antes do render. "
                        "A transcrição oficial do áudio divergiu do texto final aprovado."
                    )
                return {"video_url": "/media/videos/test.mp4", "render_report": {}}

        return FakeGenerator

    def test_checkpoint_is_emitted_before_later_audio_critic_failure(self):
        audio_path = self._audio_file()
        FakeGenerator = self._fake_generator_class(audio_path, fail_after_audio=True)
        persisted = []

        with mock.patch.object(ac, "_persist_checkpoint", side_effect=lambda _g, cp, **kw: persisted.append((dict(cp), dict(kw))) or True), \
             mock.patch.object(ac, "_checkpoint_from_task", side_effect=lambda _tid: dict(persisted[-1][0]) if persisted else {}):
            ac.install_audio_checkpoint_patch(FakeGenerator)
            generator = FakeGenerator()
            plan = {"scenes": [{"text": "Texto final aprovado para a narração."}]}
            with self.assertRaisesRegex(RuntimeError, "Diagnóstico TTS"):
                generator.create_video_from_plan(plan)

        self.assertGreaterEqual(len(persisted), 2)
        generated, generated_meta = persisted[0]
        failed, failed_meta = persisted[-1]
        self.assertEqual(generated["checkpoint_status"], "generated")
        self.assertEqual(generated["output_path"], audio_path)
        self.assertGreater(generated["audio_size_bytes"], 1000)
        self.assertEqual(generated["provider_used"], "edge_tts")
        self.assertEqual(generated["configured_provider"], "elevenlabs")
        self.assertTrue(generated["fallback_used"])
        self.assertIn("quota unavailable", generated["fallback_reason"])
        self.assertFalse(generated_meta.get("failed", False))
        self.assertEqual(failed["validation_status"], "rejected")
        self.assertTrue(failed_meta.get("failed"))

    def test_orphan_or_changed_seed_is_not_reused(self):
        audio_path = self._audio_file(b"same-file" * 300)
        file_hash = ac._file_sha256(audio_path)
        checkpoint = {
            "output_path": audio_path,
            "audio_sha256": file_hash,
            "final_text_sent_to_tts": "Roteiro antigo completamente diferente do atual.",
        }

        class FakeGenerator:
            def __init__(self):
                self.ai_service = _FakeAI("task-safe-seed")

            def _compose_segmented_narration_audio(self, **kwargs):
                return {}

            def create_video_from_plan(self, plan, *args, **kwargs):
                return {
                    "seed_present": bool(plan.get("seed_audio_path")),
                    "render_only": bool(plan.get("force_render_only")),
                    "reuse_assets": bool(plan.get("force_reuse_assets")),
                }

        with mock.patch.object(ac, "_checkpoint_from_task", return_value=checkpoint), \
             mock.patch.object(ac, "_persist_checkpoint", return_value=True), \
             mock.patch("app.services.task_manager.merge_task_result", return_value={}):
            ac.install_audio_checkpoint_patch(FakeGenerator)
            plan = {
                "seed_audio_path": audio_path,
                "seed_narration_text": checkpoint["final_text_sent_to_tts"],
                "force_render_only": True,
                "scenes": [{"text": "Um roteiro novo sobre outro assunto sem relação com o áudio antigo."}],
            }
            result = FakeGenerator().create_video_from_plan(plan)

        self.assertFalse(result["seed_present"])
        self.assertFalse(result["render_only"])
        self.assertTrue(result["reuse_assets"])

    def test_same_task_same_file_and_compatible_text_keeps_seed(self):
        audio_path = self._audio_file(b"verified-audio" * 300)
        expected_text = "Jesus ensinou sobre fé esperança e perseverança em tempos difíceis."
        checkpoint = {
            "output_path": audio_path,
            "audio_sha256": ac._file_sha256(audio_path),
            "final_text_sent_to_tts": expected_text,
        }

        class FakeGenerator:
            def __init__(self):
                self.ai_service = _FakeAI("task-compatible-seed")

            def _compose_segmented_narration_audio(self, **kwargs):
                return {}

            def create_video_from_plan(self, plan, *args, **kwargs):
                return {"seed_audio_path": plan.get("seed_audio_path")}

        with mock.patch.object(ac, "_checkpoint_from_task", return_value=checkpoint), \
             mock.patch.object(ac, "_persist_checkpoint", return_value=True):
            ac.install_audio_checkpoint_patch(FakeGenerator)
            plan = {
                "seed_audio_path": audio_path,
                "seed_narration_text": expected_text,
                "force_render_only": True,
                "scenes": [{"text": expected_text}],
            }
            result = FakeGenerator().create_video_from_plan(plan)

        self.assertEqual(result["seed_audio_path"], audio_path)

    def test_file_hash_detects_changed_audio(self):
        audio_path = self._audio_file(b"before" * 300)
        original_hash = ac._file_sha256(audio_path)
        with open(audio_path, "ab") as fh:
            fh.write(b"changed")
        self.assertNotEqual(original_hash, ac._file_sha256(audio_path))


if __name__ == "__main__":
    unittest.main()
