import unittest
from pathlib import Path


class WorkerNarrationCodeGuardTests(unittest.TestCase):
    def test_real_worker_installs_narration_guard_and_fails_closed(self):
        source = Path("app/worker.py").read_text(encoding="utf-8")
        self.assertIn("from app.services.narration_contract_guard import install_narration_contract_guard", source)
        self.assertIn("install_narration_contract_guard(video_generator_cls)", source)
        self.assertIn('getattr(video_generator_cls, "_codexia_narration_core_v1"', source)
        self.assertIn("worker recusou iniciar para não narrar códigos", source)

    def test_legacy_audio_without_plain_text_contract_is_not_reused(self):
        source = Path("app/services/audio_checkpoint.py").read_text(encoding="utf-8")
        self.assertIn('reason = "legacy_audio_without_spoken_text_contract"', source)
        self.assertIn('checkpoint.get("tts_plain_text_only") is not True', source)
        self.assertIn("_mark_audio_rejected(seed_path, reason)", source)

    def test_narration_contract_metadata_is_recorded_after_provider_call(self):
        source = Path("app/services/narration_contract_guard.py").read_text(encoding="utf-8")
        provider_call = source.index("output = original_generate_audio(self, clean")
        metadata_write = source.index('"spoken_text_sha256": build_narration_artifact(clean).text_sha256')
        self.assertLess(provider_call, metadata_write)


if __name__ == "__main__":
    unittest.main()
