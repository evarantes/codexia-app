import unittest
from pathlib import Path
from types import SimpleNamespace

from app.models import UnifiedVideoStatus
from app.services.unified_video_pipeline import UnifiedVideoRequest
from app.services.youtube_channel_context import (
    channel_context_text_from_snapshot,
    sanitize_channel_snapshot,
)
from app.services.youtube_publication_reconciler import _publishable_status


ROOT = Path(__file__).resolve().parents[1]


class YouTubeDecouplingContractTests(unittest.TestCase):
    def _request(self, auto_publish: bool) -> UnifiedVideoRequest:
        return UnifiedVideoRequest(
            source_module="test",
            source_id="video:contract",
            idempotency_key="contract:youtube-decoupling",
            content_type="story",
            topic="Mesmo vídeo",
            duration_minutes=2,
            auto_publish=auto_publish,
            review_required=False,
        )

    def test_publication_intent_does_not_change_video_identity(self):
        manual = self._request(False)
        automatic = self._request(True)
        self.assertEqual(manual.canonical_payload(), automatic.canonical_payload())
        self.assertEqual(manual.request_hash_hex(), automatic.request_hash_hex())
        self.assertFalse(manual.auto_publish)
        self.assertTrue(automatic.auto_publish)

    def test_channel_snapshot_strips_credentials_but_keeps_editorial_stats(self):
        snapshot = sanitize_channel_snapshot({
            "connected": True,
            "channel_title": "Canal Teste",
            "subscribers": 123,
            "views": 456,
            "client_id": "nao-pode-persistir",
            "refresh_token": "nao-pode-persistir",
            "nested": {"access_token": "nao", "safe": "sim"},
        })
        self.assertNotIn("client_id", snapshot)
        self.assertNotIn("refresh_token", snapshot)
        self.assertEqual(snapshot["nested"], {"safe": "sim"})
        self.assertEqual(snapshot["subscribers"], 123)
        text = channel_context_text_from_snapshot(snapshot)
        self.assertIn("Canal Teste", text)
        self.assertIn("123", text)

    def test_only_ready_authorized_items_are_publication_candidates(self):
        approved = SimpleNamespace(status=UnifiedVideoStatus.APPROVED, review_required=True)
        awaiting_no_review = SimpleNamespace(status=UnifiedVideoStatus.AWAITING_REVIEW, review_required=False)
        awaiting_review = SimpleNamespace(status=UnifiedVideoStatus.AWAITING_REVIEW, review_required=True)
        rendering = SimpleNamespace(status=UnifiedVideoStatus.RENDERING, review_required=False)
        self.assertTrue(_publishable_status(approved))
        self.assertTrue(_publishable_status(awaiting_no_review))
        self.assertFalse(_publishable_status(awaiting_review))
        self.assertFalse(_publishable_status(rendering))

    def test_build_hardening_removed_raw_ui_and_localhost_oauth_contracts(self):
        index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        pipeline = (ROOT / "app/services/unified_video_pipeline.py").read_text(encoding="utf-8")

        self.assertIn("tailwind-ready", index)
        self.assertNotIn('src="https://cdn.tailwindcss.com" defer', index)
        self.assertNotIn("http://127.0.0.1:8010/youtube/auth/callback", index)
        self.assertIn("redirect_base = \"/\"", router)
        self.assertIn("reconcile_pending_youtube_publications", router)
        self.assertIn('merged.setdefault("auto_upload", False)', pipeline)
        self.assertIn("production_preserved", pipeline)


if __name__ == "__main__":
    unittest.main()
