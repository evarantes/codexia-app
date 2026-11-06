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

    def test_upload_and_user_scoped_paths(self):
        result = store_host_reference(user_id=42, host="Helena", body=self.image)
        self.assertTrue(result["private"])
        self.assertTrue(host_reference_status(user_id=42)["Helena"])
        self.assertFalse(host_reference_status(user_id=43)["Helena"])
        self.assertTrue(reference_file(user_id=42, host="Helena").is_file())
        with Image.open(reference_file(user_id=42, host="Helena")) as image:
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
        result = private_storage_health()
        self.assertEqual(result, {"ready": True, "reason": "write_access_verified"})
        self.assertNotIn("CODEXIA_STUDIO_PRIVATE_DIR", str(result))

    def test_public_static_directory_fails_probe(self):
        public_dir = Path(__file__).resolve().parents[1] / "app" / "static"
        with patch.dict(os.environ, {"CODEXIA_STUDIO_PRIVATE_DIR": str(public_dir)}):
            result = private_storage_health()
        self.assertEqual(result, {"ready": False, "reason": "public_directory"})

    def test_no_env_variable_fails_probe(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(private_storage_health()["reason"], "missing_configuration")

    def test_no_volume_configured_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StudioReferenceError):
                reference_file(user_id=42, host="Helena")


if __name__ == "__main__":
    unittest.main()
