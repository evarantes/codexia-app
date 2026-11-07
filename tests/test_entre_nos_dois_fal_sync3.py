import tempfile
import unittest
from pathlib import Path

from app.services.entre_nos_dois_fal_sync3 import prepare_fal_sync3_preview
from app.services.entre_nos_dois_lipsync_gate import LipSyncGateError
from app.services.entre_nos_dois_timeline import build_measured_timeline
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class FalSync3PreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        plan = prepare_speech_manifest()
        clips = []
        for segment in plan["segments"]:
            f = Path(temp.name) / f'{segment["index"]}.mp3'
            f.write_bytes(b"ID3" + b"0" * 2048)
            clips.append({"index": segment["index"], "speaker": segment["speaker"], "path": str(f)})
        self.timeline = build_measured_timeline({
            "program": "entre-nos-dois", "project_id": 42,
            "approval_status": "awaiting_human_review", "segments": clips,
        }, probe=lambda path: 6.0)
        self.refs = {
            "Gabriel": "projects/42/hosts/gabriel.png",
            "Helena": "projects/42/hosts/helena.png",
        }
        self.assets = {1: {
            "image_url": "https://assets.example.com/signed/helena.png?token=test",
            "audio_url": "https://assets.example.com/signed/helena.mp3?token=test",
        }}

    def prepare(self, **extra):
        return prepare_fal_sync3_preview(
            timeline=self.timeline, reference_keys=self.refs,
            asset_urls=self.assets, **extra
        )

    def test_unapproved_returns_price_only_without_media_url(self):
        result = self.prepare()
        self.assertEqual(result["status"], "awaiting_asset_and_cost_approval")
        self.assertNotIn("request_input", result)
        self.assertFalse(result["billable_calls_performed"])
        self.assertEqual(result["character"], "Helena")

    def test_first_scene_contract(self):
        result = self.prepare(approved_max_usd=1.0)
        self.assertEqual(result["model"], "fal-ai/sync-lipsync/v3/image-to-video")
        self.assertEqual(result["request_input"]["enable_safety_checker"], True)
        self.assertEqual(result["status"], "ready_for_explicit_submission")
        self.assertFalse(result["billable_calls_performed"])

    def test_budget_blocks_preview(self):
        with self.assertRaises(LipSyncGateError):
            self.prepare(approved_max_usd=0.1)

    def test_rejects_private_asset_network(self):
        self.assets[1]["image_url"] = "https://127.0.0.1/private.png"
        with self.assertRaises(LipSyncGateError):
            self.prepare(approved_max_usd=1.0)

    def test_rejects_credentials_in_asset_url(self):
        self.assets[1]["audio_url"] = "https://user:pass@assets.example.com/signed.mp3"
        with self.assertRaises(LipSyncGateError):
            self.prepare(approved_max_usd=1.0)


if __name__ == "__main__":
    unittest.main()
