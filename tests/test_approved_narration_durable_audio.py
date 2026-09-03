import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.production_manifest import record_artifact


ROOT = Path(__file__).resolve().parents[1]
HARDENING_PATH = ROOT / "scripts" / "apply_youtube_narration_gate.py"


def _load_hardening_module():
    spec = importlib.util.spec_from_file_location("approved_narration_hardening", HARDENING_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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

    def test_hardening_upgrades_existing_core_block_to_durable_contract(self):
        hardening = _load_hardening_module()
        old_source = (
            "prefix\n"
            "        # narration-core-approved-audio-reuse-v1\n"
            "        old approved audio block\n"
            + hardening.RENDER_CALL_MARKER
            + "            script\n"
            "suffix\n"
        )
        upgraded = hardening._replace_existing_router_block(old_source)

        self.assertIn(hardening.APPROVED_AUDIO_DURABLE_MARKER, upgraded)
        self.assertIn("_record_production_artifact(", upgraded)
        self.assertIn('source="approved_narration_core_v1"', upgraded)
        self.assertIn('script["seed_audio_path"] = str(durable_audio_path)', upgraded)
        self.assertIn('"tts_regeneration_allowed": False', upgraded)
        self.assertNotIn("old approved audio block", upgraded)
        self.assertEqual(upgraded.count(hardening.RENDER_CALL_MARKER), 1)

    def test_durable_block_fails_closed_if_manifest_persistence_fails(self):
        hardening = _load_hardening_module()
        block = hardening.APPROVED_AUDIO_BLOCK_CORE
        self.assertIn("não pôde ser preservado no manifesto da tarefa", block)
        self.assertIn("nenhum novo TTS será criado", block)
        self.assertIn('script["approved_narration_required"] = True', block)
        self.assertIn('"manifest_persisted": True', block)


if __name__ == "__main__":
    unittest.main()
