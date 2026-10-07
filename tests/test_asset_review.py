import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from app.services.asset_review import apply_manual_review, video_fingerprint
from app.routers import cinematic_queue as cq
from app.services.unified_video_pipeline import UnifiedVideoPipelineService


class AssetReviewTests(unittest.TestCase):
    def test_override_is_scoped_and_expires_when_file_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"original")
            record = {"reviewed_by": 7, "video_sha256": video_fingerprint(str(path))}
            review = {"asset_approvals": {"images": record}}
            checks = {"visual_variety_valid": False, "caption_sync_valid": False, "mp4_exists": False}
            applied = apply_manual_review(checks, review, str(path))
            self.assertEqual(set(applied), {"visual_variety_valid"})
            self.assertTrue(checks["visual_variety_valid"])
            self.assertFalse(checks["caption_sync_valid"])
            self.assertFalse(checks["mp4_exists"])
            path.write_bytes(b"replacement")
            checks["visual_variety_valid"] = False
            self.assertEqual(apply_manual_review(checks, review, str(path)), {})
            self.assertFalse(checks["visual_variety_valid"])

    def test_global_approval_keeps_physical_checks_and_requires_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"original")
            record = {"video_sha256": video_fingerprint(str(path))}
            review = {"asset_approvals": {"all": record}}
            checks = {"visual_variety_valid": False, "mp4_exists": False}
            self.assertEqual(apply_manual_review(checks, review, str(path)), {})
            record["reviewed_by"] = 7
            apply_manual_review(checks, review, str(path))
            self.assertTrue(checks["visual_variety_valid"])
            self.assertFalse(checks["mp4_exists"])

    def _fixtures(self):
        result = {"video_url": "/videos/existing.mp4", "script": {"title": "Original"}, "selected_images": ["original.png"], "error": "old warning"}
        row = SimpleNamespace(id="task", status="failed", progress=100, message="old warning", result_json=json.dumps(result))
        unified = SimpleNamespace(video_path="/videos/existing.mp4", video_url="/videos/existing.mp4", review_feedback_json="{}", status="failed", last_error="old warning", auto_publish=True)
        checks = {"visual_variety_valid": False, "caption_sync_valid": True, "mp4_exists": True, "ffprobe_has_video_stream": True, "duration_valid": True}
        validation = SimpleNamespace(ok=False, checks=checks, first_failed="visual_variety_valid", details={"automatic_checks": checks.copy()})
        service = MagicMock()
        service.validate_before_awaiting_review.return_value = validation
        return result, row, unified, service

    def test_approve_existing_failed_video_records_audit_without_generation(self):
        original, row, unified, service = self._fixtures()
        with patch.object(cq, "_registered_owned_task", return_value=(row, {})), \
                patch.object(cq, "_unified_for_task", return_value=unified), \
                patch.object(cq, "unified_video_pipeline", return_value=service), \
                patch.object(cq, "_ffprobe_streams", return_value={"has_video": True, "video_duration": 102}), \
                patch.object(cq, "video_fingerprint", return_value="digest"), \
                patch.object(cq, "_task_to_public", return_value={}), \
                patch.object(cq, "update_task"):
            response = cq.approve_v2_project("task", cq.ReviewDecisionRequest(approve_anyway=True), MagicMock(), SimpleNamespace(id=7))
        saved = json.loads(row.result_json)
        self.assertTrue(response["approved"])
        self.assertEqual(row.status, "approved")
        self.assertEqual(saved["script"], original["script"])
        self.assertEqual(saved["selected_images"], original["selected_images"])
        self.assertEqual(saved["review"]["asset_approvals"]["all"]["reviewed_by"], 7)
        self.assertFalse(unified.auto_publish)
        self.assertEqual([call[0] for call in service.method_calls], ["validate_before_awaiting_review"])

    def test_missing_video_cannot_be_manually_approved(self):
        _, row, unified, service = self._fixtures()
        with patch.object(cq, "_registered_owned_task", return_value=(row, {})), \
                patch.object(cq, "_unified_for_task", return_value=unified), \
                patch.object(cq, "unified_video_pipeline", return_value=service), \
                patch.object(cq, "_ffprobe_streams", return_value={}), \
                patch.object(cq, "video_fingerprint", return_value=""):
            with self.assertRaises(HTTPException) as error:
                cq.approve_v2_project("task", cq.ReviewDecisionRequest(approve_anyway=True), MagicMock(), SimpleNamespace(id=7))
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(row.status, "failed")

    def test_selected_verification_does_not_restart_or_change_other_assets(self):
        original, row, unified, service = self._fixtures()
        service.validate_before_awaiting_review.return_value.checks.update({"audio_exists_and_non_empty": True, "duration_matches_request": True, "duration_covers_narration": True})
        service.validate_before_awaiting_review.return_value.details["automatic_checks"] = service.validate_before_awaiting_review.return_value.checks.copy()
        with patch.object(cq, "_registered_owned_task", return_value=(row, {})), \
                patch.object(cq, "_unified_for_task", return_value=unified), \
                patch.object(cq, "unified_video_pipeline", return_value=service), \
                patch.object(cq, "video_fingerprint", return_value="digest"), \
                patch.object(cq, "_task_to_public", return_value={}), \
                patch.object(cq, "update_task"):
            response = cq.review_v2_asset("task", "narration", cq.AssetReviewRequest(), MagicMock(), SimpleNamespace(id=7))
        saved = json.loads(row.result_json)
        self.assertTrue(response["ok"])
        self.assertEqual(set(saved["asset_verifications"]), {"narration"})
        self.assertEqual(saved["script"], original["script"])
        self.assertEqual(saved["selected_images"], original["selected_images"])
        self.assertEqual(row.status, "failed")
        self.assertEqual([call[0] for call in service.method_calls], ["validate_before_awaiting_review"])

    def test_active_task_cannot_be_approved_or_modified(self):
        _, row, _, _ = self._fixtures()
        row.status = "processing"
        with patch.object(cq, "_registered_owned_task", return_value=(row, {})):
            with self.assertRaises(HTTPException):
                cq.review_v2_asset("task", "render", cq.AssetReviewRequest(action="approve"), MagicMock(), SimpleNamespace(id=7))

    def test_replaced_manually_approved_video_is_not_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"original")
            review = {"asset_approvals": {"all": {"reviewed_by": 7, "video_sha256": video_fingerprint(str(path))}}}
            unified = SimpleNamespace(status="approved", youtube_video_id=None, video_path=str(path), review_feedback_json=json.dumps(review))
            path.write_bytes(b"replacement")
            service = UnifiedVideoPipelineService()
            upload = MagicMock()
            with patch.object(service, "_find_any", return_value=unified):
                result = service.publish_if_ready(MagicMock(), "task", upload_callable=upload)
            self.assertEqual(result["code"], "manual_approval_stale")
            upload.assert_not_called()


class PublicationFailureTests(unittest.TestCase):
    def _publish(self, response):
        unified = SimpleNamespace(status="approved", youtube_video_id=None,
            video_path="/videos/existing.mp4", review_feedback_json="{}",
            task_id="task", idempotency_key="task", visibility="unlisted")
        service = UnifiedVideoPipelineService()
        with patch.object(service, "_find_any", return_value=unified), \
                patch.object(service, "_file_exists_or_url", return_value=True), \
                patch.object(service, "transition_status") as transition:
            result = service.publish_if_ready(MagicMock(), "task",
                upload_callable=MagicMock(return_value=response))
        return result, transition.call_args.kwargs

    def test_disconnected_channel_keeps_real_reason_and_approved_media(self):
        result, saved = self._publish({"error": "Autorização expirada", "status": "not_connected"})
        self.assertFalse(result["ok"])
        self.assertIn("Autorização expirada", result["error"])
        self.assertIn("Configurações > YouTube", result["error"])
        self.assertEqual(saved["status"], "approved")
        self.assertEqual(saved["progress"], 100)
        self.assertTrue(saved["merge_result"]["production_preserved"])
        self.assertTrue(saved["merge_result"]["publish_pending"])

    def test_provider_failure_is_not_replaced_by_missing_id(self):
        result, saved = self._publish({"error": "quotaExceeded"})
        self.assertIn("quotaExceeded", result["error"])
        self.assertEqual(result["code"], "publication_pending")
        self.assertEqual(saved["status"], "approved")

    def test_unknown_upload_result_requires_studio_check_before_retry(self):
        result, saved = self._publish({})
        self.assertEqual(result["code"], "no_video_id")
        self.assertIn("YouTube Studio", result["error"])
        self.assertEqual(saved["status"], "approved")

    def test_confirmed_upload_still_publishes(self):
        result, saved = self._publish({"id": "confirmed-id"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["youtube_video_id"], "confirmed-id")
        self.assertEqual(saved["status"], "published")
