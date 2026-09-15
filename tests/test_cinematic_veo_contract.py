import os
import unittest
from unittest.mock import Mock, patch

from app.services.cinematic_video_provider import CinematicVideoProvider


class CinematicVeoContractTests(unittest.TestCase):
    def test_submit_veo_omits_number_of_videos_and_keeps_supported_parameters(self):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = ""
        response.json.return_value = {"name": "operations/veo-test-1"}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False), patch(
            "app.services.cinematic_video_provider.requests.post", return_value=response
        ) as post:
            provider = CinematicVideoProvider()
            result = provider.submit_veo(
                prompt="Cinematic valley at dawn",
                duration=8,
                premium=True,
                aspect_ratio="16:9",
            )

        self.assertEqual(result["job_id"], "operations/veo-test-1")
        args, kwargs = post.call_args
        self.assertIn("veo-3.1-fast-generate-preview:predictLongRunning", args[0])
        payload = kwargs["json"]
        self.assertEqual(payload["instances"][0]["prompt"], "Cinematic valley at dawn")
        self.assertNotIn("numberOfVideos", payload["parameters"])
        self.assertEqual(payload["parameters"]["durationSeconds"], 8)
        self.assertIsInstance(payload["parameters"]["durationSeconds"], int)
        self.assertEqual(payload["parameters"]["aspectRatio"], "16:9")
        self.assertEqual(payload["parameters"]["resolution"], "720p")

    def test_submit_veo_preserves_image_input_and_portrait_ratio(self):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = ""
        response.json.return_value = {"name": "operations/veo-test-2"}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False), patch(
            "app.services.cinematic_video_provider.requests.post", return_value=response
        ) as post:
            provider = CinematicVideoProvider()
            provider.submit_veo(
                prompt="David walks across the valley",
                image_base64="ZmFrZS1pbWFnZQ==",
                image_mime="image/png",
                duration=5,
                premium=False,
                aspect_ratio="9:16",
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["instances"][0]["image"],
            {"inlineData": {"mimeType": "image/png", "data": "ZmFrZS1pbWFnZQ=="}},
        )
        self.assertEqual(payload["parameters"]["durationSeconds"], 4)
        self.assertIsInstance(payload["parameters"]["durationSeconds"], int)
        self.assertEqual(payload["parameters"]["aspectRatio"], "9:16")
        self.assertNotIn("numberOfVideos", payload["parameters"])

    def test_poll_veo_surfaces_operation_error(self):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = ""
        response.json.return_value = {
            "done": True,
            "error": {"code": 400, "message": "generation blocked"},
        }

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False), patch(
            "app.services.cinematic_video_provider.requests.get", return_value=response
        ):
            result = CinematicVideoProvider().poll_veo("operations/veo-failed")

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "generation blocked")
        self.assertIsNone(result["output_url"])


if __name__ == "__main__":
    unittest.main()
