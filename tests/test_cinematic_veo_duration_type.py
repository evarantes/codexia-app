import unittest
from unittest.mock import Mock, patch

from app.services.cinematic_video_provider import CinematicVideoProvider


class VeoDurationTypeTests(unittest.TestCase):
    def test_duration_clamp_returns_int(self):
        self.assertEqual(CinematicVideoProvider._veo_duration_value(3), 4)
        self.assertEqual(CinematicVideoProvider._veo_duration_value(5), 4)
        self.assertEqual(CinematicVideoProvider._veo_duration_value(6), 6)
        self.assertEqual(CinematicVideoProvider._veo_duration_value(7), 6)
        self.assertEqual(CinematicVideoProvider._veo_duration_value(8), 8)
        self.assertIsInstance(CinematicVideoProvider._veo_duration_value(8), int)

    def test_submit_veo_sends_duration_seconds_as_json_number(self):
        response = Mock()
        response.ok = True
        response.json.return_value = {"name": "operations/test-veo-job"}

        provider = CinematicVideoProvider(settings=None)
        with patch.object(provider, "_gemini_key", return_value="test-key"), patch(
            "app.services.cinematic_video_provider.requests.post", return_value=response
        ) as post:
            result = provider.submit_veo(
                prompt="cinematic valley at dawn",
                duration=8,
                premium=False,
                aspect_ratio="16:9",
            )

        payload = post.call_args.kwargs["json"]
        duration = payload["parameters"]["durationSeconds"]
        self.assertEqual(duration, 8)
        self.assertIsInstance(duration, int)
        self.assertNotIsInstance(duration, str)
        self.assertNotIn("numberOfVideos", payload["parameters"])
        self.assertEqual(result["job_id"], "operations/test-veo-job")


if __name__ == "__main__":
    unittest.main()
