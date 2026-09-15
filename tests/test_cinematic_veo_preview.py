import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.services.cinematic_video_provider import CinematicVideoProvider


class VeoPilotPreviewTests(unittest.TestCase):
    def test_completed_veo_job_is_downloaded_to_local_media(self):
        status_response = MagicMock()
        status_response.ok = True
        status_response.json.return_value = {
            "done": True,
            "response": {
                "generateVideoResponse": {
                    "generatedSamples": [
                        {"video": {"uri": "https://example.invalid/generated-video"}}
                    ]
                }
            },
        }

        download_response = MagicMock()
        download_response.ok = True
        download_response.__enter__.return_value = download_response
        download_response.__exit__.return_value = False
        download_response.iter_content.return_value = [b"0" * (40 * 1024)]

        provider = CinematicVideoProvider()
        job_id = "models/veo-3.1-fast-generate-preview/operations/test-preview-job"

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            provider, "_gemini_key", return_value="test-key"
        ), patch(
            "app.services.cinematic_video_provider.VIDEO_OUTPUT_DIR", tmp
        ), patch(
            "app.services.cinematic_video_provider.VIDEO_URL_PREFIX", "/media/videos"
        ), patch(
            "app.services.cinematic_video_provider.requests.get",
            side_effect=[status_response, download_response],
        ) as get:
            result = provider.poll_veo(job_id)

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertTrue(result["output_url"].startswith("/media/videos/veo_pilot_"))
        self.assertTrue(result["filename"].endswith(".mp4"))
        self.assertEqual(result["remote_output_url"], "https://example.invalid/generated-video")
        self.assertEqual(get.call_count, 2)

    def test_pending_veo_job_does_not_download(self):
        response = MagicMock()
        response.ok = True
        response.json.return_value = {"done": False}
        provider = CinematicVideoProvider()
        with patch.object(provider, "_gemini_key", return_value="test-key"), patch(
            "app.services.cinematic_video_provider.requests.get", return_value=response
        ) as get:
            result = provider.poll_veo("models/veo/operations/pending")
        self.assertEqual(result["status"], "PENDING")
        self.assertIsNone(result["output_url"])
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
