import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.services.entre_nos_dois_sync3_queue import (
    Sync3QueueError, submit_first_sync3_scene, check_sync3_status,
)
from app.services.entre_nos_dois_timeline import build_measured_timeline
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


REQUEST_ID = "764cabcf-b745-4b3e-ae38-1200304cf45b"


class Sync3QueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        chunks = []
        for seg in prepare_speech_manifest()["segments"]:
            path = Path(tmp.name) / f'{seg["index"]}.mp3'
            path.write_bytes(b"ID3" + b"x" * 2048)
            chunks.append({"index": seg["index"], "speaker": seg["speaker"], "path": str(path)})
        self.timeline = build_measured_timeline({
            "program": "entre-nos-dois", "project_id": 42,
            "approval_status": "awaiting_human_review", "segments": chunks,
        }, probe=lambda _: 6.0)
        self.refs = {
            "Gabriel": "projects/42/hosts/gabriel.png",
            "Helena": "projects/42/hosts/helena.png",
        }
        self.assets = {1: {
            "image_url": "https://assets.example.com/helena.png?sig=mock",
            "audio_url": "https://assets.example.com/helena.mp3?sig=mock",
        }}
        self.transport = Mock(return_value={"request_id": REQUEST_ID, "status": "IN_QUEUE"})

    def submit(self, **overrides):
        options = dict(
            timeline=self.timeline, reference_keys=self.refs, asset_urls=self.assets,
            approved_max_usd=1.0, api_key="fake-key", durable_receipt_dir=self.root,
            manual_confirmation=True, transport=self.transport,
        )
        options.update(overrides)
        return submit_first_sync3_scene(**options)

    def test_no_explicit_confirmation_means_no_charge(self):
        with self.assertRaises(Sync3QueueError):
            self.submit(manual_confirmation=False)
        self.transport.assert_not_called()

    def test_one_submission_and_persistent_receipt(self):
        result = self.submit()
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertEqual(self.transport.call_count, 1)
        self.assertNotIn("fake-key", str(result))
        with self.assertRaises(Sync3QueueError):
            self.submit()
        self.assertEqual(self.transport.call_count, 1)

    def test_uncertain_network_response_prevents_rebilling(self):
        self.transport.side_effect = TimeoutError("lost connection")
        with self.assertRaises(Sync3QueueError):
            self.submit()
        self.transport.side_effect = None
        with self.assertRaises(Sync3QueueError):
            self.submit()
        self.assertEqual(self.transport.call_count, 1)

    def test_over_budget_blocks_submit(self):
        with self.assertRaises(ValueError):
            self.submit(approved_max_usd=0.1)
        self.transport.assert_not_called()

    def test_queue_status_queries_do_not_submit(self):
        status_transport = Mock(return_value={"status": "IN_PROGRESS"})
        result = check_sync3_status(
            request_id=REQUEST_ID, api_key="fake-key",
            transport=status_transport,
        )
        self.assertEqual(result["status"], "IN_PROGRESS")
        self.assertFalse(result["needs_result_fetch"])
        self.assertEqual(status_transport.call_args.args[0], "GET")
        self.assertFalse(result["auto_publish"])


if __name__ == "__main__":
    unittest.main()
