import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services.entre_nos_dois_references import (
    StudioReferenceError, host_reference_status, reference_file, store_host_reference, private_storage_health,
)


class StudioReferencesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"CODEXIA_STUDIO_PRIVATE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        file = io.BytesIO()
        Image.new("RGB", (64, 64), (80, 70, 120)).save(file, format="JPEG")
        self.image = file.getvalue()
        self.assets = {}
        def save_asset(*, user_id, kind, filename, body, metadata=None):
            self.assets[(user_id, kind, filename)] = {"kind": kind, "filename": filename, "bytes": body, **(metadata or {})}
        self.save_patch = patch("app.services.entre_nos_dois_references.store_asset_bytes", side_effect=save_asset)
        self.find_patch = patch("app.services.entre_nos_dois_references.find_asset", side_effect=lambda *, user_id, kind, filename: self.assets.get((user_id, kind, filename)))
        self.save_patch.start(); self.find_patch.start()
        self.addCleanup(self.save_patch.stop); self.addCleanup(self.find_patch.stop)

    def test_upload_and_user_scoped_paths(self):
        result = store_host_reference(user_id=42, host="Helena", body=self.image)
        self.assertTrue(result["private"])
        self.assertTrue(host_reference_status(user_id=42)["Helena"])
        self.assertFalse(host_reference_status(user_id=43)["Helena"])
        self.assertEqual(result["storage"], "google_drive")
        self.assertIn((42, "image", "helena.png"), self.assets)
        self.assertNotIn((43, "image", "helena.png"), self.assets)
        self.assertFalse(reference_file(user_id=42, host="Helena").is_file())
        with Image.open(io.BytesIO(self.assets[(42, "image", "helena.png")]["bytes"])) as image:
            self.assertEqual(image.format, "PNG")

    def test_bad_image_rejected(self):
        with self.assertRaises(StudioReferenceError):
            store_host_reference(user_id=42, host="Gabriel", body=b"invalid image")

    def test_oversized_upload_rejected(self):
        with self.assertRaises(StudioReferenceError):
            store_host_reference(user_id=42, host="Gabriel", body=b"x" * (8 * 1024 * 1024 + 1))

    def test_unknown_host_rejected(self):
        with self.assertRaises(StudioReferenceError):
            store_host_reference(user_id=42, host="../Helena", body=self.image)

    def test_private_storage_write_probe_succeeds(self):
        with patch("app.services.entre_nos_dois_references.drive_storage_health", return_value={"ready": True, "reason": "drive_configured"}):
            result = private_storage_health(user_id=42)
        self.assertEqual(result, {"ready": True, "reason": "drive_and_metadata_ready"})

    def test_public_static_directory_fails_probe(self):
        with patch("app.services.entre_nos_dois_references.drive_storage_health", return_value={"ready": False, "reason": "google_drive_not_configured"}):
            result = private_storage_health(user_id=42)
        self.assertEqual(result["reason"], "google_drive_not_configured")

    def test_no_env_variable_fails_probe(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("app.services.entre_nos_dois_references.drive_storage_health", return_value={"ready": False, "reason": "google_drive_not_configured"}):
                self.assertEqual(private_storage_health()["reason"], "google_drive_not_configured")

    def test_no_volume_configured_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StudioReferenceError):
                reference_file(user_id=42, host="Helena")


if __name__ == "__main__":
    unittest.main()
