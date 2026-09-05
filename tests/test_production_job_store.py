import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ProductionJobStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _store(self):
        from app.services.production_job_store import ProductionJobStore
        return ProductionJobStore(output_root=self.tmp.name)

    def _preview_files(self, preview_id="a" * 32):
        root = Path(self.tmp.name) / "source"
        root.mkdir(parents=True, exist_ok=True)
        mp3 = root / f"{preview_id}.mp3"
        meta = root / f"{preview_id}.json"
        mp3.write_bytes(b"ID3" + b"x" * 2048)
        meta.write_text(json.dumps({
            "preview_id": preview_id,
            "approved": True,
            "text_sha256": "b" * 64,
            "spoken_text_sent_to_tts": "Jesus é o motivo de eu existir.",
            "narration_core_version": 1,
            "narration_core_namespace": "codexia-narration-core-v1",
        }), encoding="utf-8")
        return mp3, meta

    def test_job_gets_own_folder_and_approved_mp3(self):
        store = self._store()
        preview_id = "a" * 32
        mp3, meta = self._preview_files(preview_id)
        job = store.register_preview(
            user_id=7,
            source_mp3=mp3,
            source_meta=meta,
            preview_id=preview_id,
            theme="Jesus, o motivo de eu existir",
        )
        job_id = job["job_id"]
        self.assertTrue(job_id.startswith("YT-"))
        self.assertEqual(job["status"], "awaiting_narration_review")
        approved = store.approve_preview(user_id=7, job_id=job_id, preview_id=preview_id)
        self.assertTrue(approved["tts_locked"])
        self.assertEqual(approved["status"], "narration_approved")
        self.assertTrue(Path(approved["approved_audio_path"]).is_file())
        self.assertEqual(Path(approved["approved_audio_path"]).name, "approved_narration.mp3")
        validated = store.validated_approved_audio(user_id=7, job_id=job_id)
        self.assertEqual(validated["job"]["approved_preview_id"], preview_id)
        self.assertTrue(validated["meta"]["approved"])

    def test_redo_stays_in_same_job_and_creates_new_version(self):
        store = self._store()
        mp3a, metaa = self._preview_files("a" * 32)
        job = store.register_preview(user_id=3, source_mp3=mp3a, source_meta=metaa, preview_id="a" * 32, theme="Tema")
        job_id = job["job_id"]

        mp3b, metab = self._preview_files("c" * 32)
        job2 = store.register_preview(user_id=3, source_mp3=mp3b, source_meta=metab, preview_id="c" * 32, job_id=job_id)
        self.assertEqual(job2["job_id"], job_id)
        self.assertEqual(len(job2["narration_versions"]), 2)
        self.assertEqual(job2["narration_versions"][0]["version"], 1)
        self.assertEqual(job2["narration_versions"][1]["version"], 2)

    def test_tampered_approved_mp3_fails_closed_before_images(self):
        store = self._store()
        preview_id = "a" * 32
        mp3, meta = self._preview_files(preview_id)
        job = store.register_preview(user_id=9, source_mp3=mp3, source_meta=meta, preview_id=preview_id)
        approved = store.approve_preview(user_id=9, job_id=job["job_id"], preview_id=preview_id)
        Path(approved["approved_audio_path"]).write_bytes(b"tampered" * 200)
        from app.services.production_job_store import ProductionJobStoreError
        with self.assertRaises(ProductionJobStoreError):
            store.validated_approved_audio(user_id=9, job_id=job["job_id"])


if __name__ == "__main__":
    unittest.main()
