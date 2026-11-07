import unittest
from unittest.mock import Mock

from app.services.entre_nos_dois_sync3_queue import Sync3QueueError
from app.services.entre_nos_dois_sync3_result import fetch_sync3_result


RID = "764cabcf-b745-4b3e-ae38-1200304cf45b"


class Sync3ResultTests(unittest.TestCase):
    def test_complete_video_remains_unapproved(self):
        transport = Mock(side_effect=[
            {"status": "COMPLETED"},
            {"video": {"url": "https://cdn.example.com/clip.mp4", "content_type": "video/mp4"}},
        ])
        result = fetch_sync3_result(request_id=RID, api_key="fake", transport=transport)
        self.assertEqual(result["status"], "awaiting_manual_video_review")
        self.assertFalse(result["approved_for_publication"])
        self.assertFalse(result["auto_publish"])
        self.assertEqual(result["additional_billable_requests"], 0)
        self.assertEqual([c.args[0] for c in transport.call_args_list], ["GET", "GET"])

    def test_no_result_request_before_complete(self):
        transport = Mock(return_value={"status": "IN_PROGRESS"})
        with self.assertRaises(Sync3QueueError):
            fetch_sync3_result(request_id=RID, api_key="fake", transport=transport)
        self.assertEqual(transport.call_count, 1)

    def test_provider_error_blocks_result(self):
        transport = Mock(return_value={"status": "COMPLETED", "error": "failed"})
        with self.assertRaises(Sync3QueueError):
            fetch_sync3_result(request_id=RID, api_key="fake", transport=transport)
        self.assertEqual(transport.call_count, 1)

    def test_private_address_is_rejected(self):
        transport = Mock(side_effect=[
            {"status": "COMPLETED"},
            {"video": {"url": "https://127.0.0.1/clip.mp4", "content_type": "video/mp4"}},
        ])
        with self.assertRaises(Sync3QueueError):
            fetch_sync3_result(request_id=RID, api_key="fake", transport=transport)

    def test_unexpected_format_rejected(self):
        transport = Mock(side_effect=[
            {"status": "COMPLETED"},
            {"video": {"url": "https://cdn.example.com/clip.html", "content_type": "text/html"}},
        ])
        with self.assertRaises(Sync3QueueError):
            fetch_sync3_result(request_id=RID, api_key="fake", transport=transport)


if __name__ == "__main__":
    unittest.main()
