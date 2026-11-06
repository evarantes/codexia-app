import os
import tempfile
import unittest
from unittest.mock import patch

from app.services.entre_nos_dois_scene_readiness import (
    LipSyncReadinessError, lip_sync_readiness,
)
from app.services.entre_nos_dois_audio_review import store_segment_audio
from app.services.entre_nos_dois_references import store_host_reference
from PIL import Image
import io


class SceneReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {"CODEXIA_STUDIO_PRIVATE_DIR": temporary.name})
        env.start()
        self.addCleanup(env.stop)
        self.assets = {}
        def save_asset(*, user_id, kind, filename, body, metadata=None):
            self.assets[(user_id, kind, filename)] = {"kind": kind, "filename": filename, "sha256": "test", "duration_seconds": (metadata or {}).get("duration_seconds")}
        self.save_refs = patch("app.services.entre_nos_dois_references.store_asset_bytes", side_effect=save_asset)
        self.save_audio = patch("app.services.entre_nos_dois_audio_review.store_asset_bytes", side_effect=save_asset)
        finder = lambda *, user_id, kind, filename: self.assets.get((user_id, kind, filename))
        self.find_refs = patch("app.services.entre_nos_dois_references.find_asset", side_effect=finder)
        self.find_audio = patch("app.services.entre_nos_dois_audio_review.find_asset", side_effect=finder)
        for mocked in (self.save_refs, self.save_audio, self.find_refs, self.find_audio): mocked.start()
        self.addCleanup(self.save_refs.stop); self.addCleanup(self.save_audio.stop)
        self.addCleanup(self.find_refs.stop); self.addCleanup(self.find_audio.stop)

    def test_missing_media_is_explicit_and_not_billable(self):
        result = lip_sync_readiness(user_id=42)
        self.assertFalse(result["inputs_ready"])
        self.assertEqual(len(result["missing"]), 6)
        self.assertIsNone(result["estimated_total_usd"])
        self.assertFalse(result["private_media_export_authorized"])
        self.assertFalse(result["generation_enabled"])
        self.assertFalse(result["auto_publish"])

    def test_four_scene_estimates_use_real_audio_lengths(self):
        frame = io.BytesIO()
        Image.new("RGB", (64, 64), (150, 140, 130)).save(frame, "PNG")
        for host in ("Gabriel", "Helena"):
            store_host_reference(user_id=42, host=host, body=frame.getvalue())
        with patch("app.services.entre_nos_dois_audio_review._duration_seconds", return_value=2.5):
            for index in (1, 2, 3, 4):
                store_segment_audio(user_id=42, index=index, body=b"test-audio-placeholder")
            result = lip_sync_readiness(user_id=42, usd_per_second="0.20")
        self.assertTrue(result["inputs_ready"])
        self.assertEqual(len(result["scenes"]), 4)
        self.assertEqual(result["estimated_total_usd"], "2.0000")
        self.assertEqual(result["measured_total_seconds"], 11.05)
        self.assertEqual(result["scenes"][0]["speaker"], "Helena")
        self.assertEqual(result["scenes"][1]["speaker"], "Gabriel")
        self.assertFalse(result["signed_asset_urls_created"])
        self.assertFalse(result["billable_calls_performed"])
        self.assertFalse(lip_sync_readiness(user_id=43)["inputs_ready"])

    def test_invalid_pricing_is_rejected(self):
        for value in ("", "0", "-1", "NaN", "Infinity", "100.01", "xyz"):
            with self.subTest(value=value), self.assertRaises(LipSyncReadinessError):
                lip_sync_readiness(user_id=42, usd_per_second=value)


if __name__ == "__main__":
    unittest.main()
