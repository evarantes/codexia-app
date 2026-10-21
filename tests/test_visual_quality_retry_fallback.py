"""Visual QA retries must not discard the original render on rejection."""
import os
import unittest
from unittest.mock import patch

from app.services.visual_quality_guard import install_visual_quality_guard_patch


class VisualQualityFallbackTests(unittest.TestCase):
    def test_rejected_replacement_preserves_original_path(self):
        class Generator:
            def __init__(self):
                self.calls = 0

            def _ensure_image_for_scene(self, prompt, **kwargs):
                self.calls += 1
                return "/tmp/original.png" if self.calls == 1 else "/tmp/rejected-retry.png"

            def create_video_from_plan(self, plan):
                return {"image": self._ensure_image_for_scene("scene")}

        reviews = [{"approve": False, "critical_issue_codes": ["eyes"]}] * 2
        with patch.dict(os.environ, {
            "ENABLE_STRICT_VISUAL_REJECT": "true",
            "VISUAL_QA_MAX_RETRIES": "1",
            "VISUAL_QA_FAIL_CLOSED": "false",
        }), patch("app.services.visual_quality_guard.review_generated_image", side_effect=reviews):
            install_visual_quality_guard_patch(Generator)
            generator = Generator()
            result = generator.create_video_from_plan({})
        self.assertEqual(result["image"], "/tmp/original.png")
        self.assertEqual(generator.calls, 2)

    def test_approved_replacement_is_used(self):
        class Generator:
            def __init__(self):
                self.calls = 0

            def _ensure_image_for_scene(self, prompt):
                self.calls += 1
                return f"/tmp/scene-{self.calls}.png"

            def create_video_from_plan(self, plan):
                return {"image": self._ensure_image_for_scene("scene")}

        with patch.dict(os.environ, {
            "ENABLE_STRICT_VISUAL_REJECT": "true",
            "VISUAL_QA_MAX_RETRIES": "1",
            "VISUAL_QA_FAIL_CLOSED": "false",
        }), patch("app.services.visual_quality_guard.review_generated_image", side_effect=[
            {"approve": False, "critical_issue_codes": ["eyes"]},
            {"approve": True, "critical_issue_codes": []},
        ]):
            install_visual_quality_guard_patch(Generator)
            result = Generator().create_video_from_plan({})
        self.assertEqual(result["image"], "/tmp/scene-2.png")


if __name__ == "__main__":
    unittest.main()
