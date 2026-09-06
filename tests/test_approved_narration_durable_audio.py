import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.production_manifest import record_artifact


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE_ROUTER = ROOT / "app" / "routers" / "youtube.py"


class ApprovedNarrationDurableAudioTests(unittest.TestCase):
    def test_manifest_copy_survives_original_cache_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "gate-cache"
            source_dir.mkdir(parents=True, exist_ok=True)
            source = source_dir / "approved.mp3"
            payload = (b"approved-narration-audio" * 256) + b"end"
            source.write_bytes(payload)
            expected_sha = hashlib.sha256(payload).hexdigest()

            manifest_root = root / "manifests"
            with patch.dict(
                os.environ,
                {"CODEXIA_PRODUCTION_MANIFEST_DIR": str(manifest_root)},
                clear=False,
            ):
                entry = record_artifact(
                    "task-approved-audio-test",
                    str(source),
                    kind="audio",
                    source="approved_narration_core_v1",
                )

            durable = Path(str(entry.get("durable_path") or ""))
            self.assertTrue(durable.is_file())
            self.assertNotEqual(durable.resolve(), source.resolve())
            self.assertEqual(hashlib.sha256(durable.read_bytes()).hexdigest(), expected_sha)

            source.unlink()
            self.assertFalse(source.exists())
            self.assertTrue(durable.is_file())
            self.assertEqual(hashlib.sha256(durable.read_bytes()).hexdigest(), expected_sha)

    def test_source_owned_router_preserves_job_audio_in_task_manifest(self):
        router = YOUTUBE_ROUTER.read_text(encoding="utf-8")
        self.assertIn("def _preserve_approved_narration_for_task(", router)
        self.assertIn("record_artifact(", router)
        self.assertIn('source="approved_narration_core_v1"', router)
        self.assertIn('script["seed_audio_path"] = approved_narration_contract["render_audio_path"]', router)
        self.assertIn('"manifest_persisted": True', router)
        self.assertIn('"tts_regeneration_allowed": False', router)

    def test_source_owned_router_fails_closed_if_manifest_persistence_fails(self):
        router = YOUTUBE_ROUTER.read_text(encoding="utf-8")
        self.assertIn("não pôde ser preservado no manifesto da tarefa", router)
        self.assertIn("imagens, render e novo TTS foram bloqueados", router)
        self.assertIn('script["approved_narration_required"] = True', router)
        self.assertIn('script["allow_tts_generation"] = False', router)


if __name__ == "__main__":
    unittest.main()
