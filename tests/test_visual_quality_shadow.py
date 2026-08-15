from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from app.services.visual_quality_shadow import (
    build_visual_quality_shadow_report,
    install_visual_quality_shadow_patch,
)


class VisualQualityShadowReportTests(unittest.TestCase):
    def _image(self, directory: str, name: str, color=(110, 90, 70), size=(1280, 720)) -> str:
        path = os.path.join(directory, name)
        Image.new("RGB", size, color=color).save(path, format="JPEG")
        return path

    def test_valid_local_image_is_measured_without_paid_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._image(tmp, "scene.jpg")
            report = build_visual_quality_shadow_report({
                "scene_visuals": [{
                    "scene_number": 1,
                    "image_path": path,
                    "source": "generated_group",
                    "final_visual_duration_sec": 5.0,
                    "max_visual_hold_sec": 5.0,
                }],
                "visual_plan": {"average_image_duration_sec": 5.0},
                "sync_validation": {"max_visual_hold_target_sec": 7.0},
            })

        self.assertEqual(report["mode"], "shadow")
        self.assertFalse(report["blocking"])
        self.assertEqual(report["paid_ai_calls"], 0)
        self.assertEqual(report["scene_count"], 1)
        self.assertEqual(report["scenes"][0]["local_metrics"]["width"], 1280)
        self.assertEqual(report["scenes"][0]["anatomy_ai_review"], "not_run")
        self.assertGreater(report["scenes"][0]["score"], 8.0)

    def test_near_duplicate_visuals_are_reported_but_never_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._image(tmp, "a.jpg", color=(100, 80, 60))
            b = self._image(tmp, "b.jpg", color=(100, 80, 60))
            report = build_visual_quality_shadow_report({
                "scene_visuals": [
                    {"scene_number": 1, "image_path": a, "final_visual_duration_sec": 5.0},
                    {"scene_number": 2, "image_path": b, "final_visual_duration_sec": 5.0},
                ],
                "visual_plan": {"average_image_duration_sec": 5.0},
            })

        self.assertFalse(report["blocking"])
        self.assertGreaterEqual(report["near_duplicate_pair_count"], 1)
        codes = [flag["code"] for flag in report["scenes"][1]["flags"]]
        self.assertIn("near_duplicate_visual", codes)

    def test_deleted_temp_artifact_is_not_misclassified_as_corruption(self):
        report = build_visual_quality_shadow_report({
            "scene_visuals": [{
                "scene_number": 1,
                "image_path": "/tmp/codexia-already-cleaned.jpg",
                "final_visual_duration_sec": 4.0,
            }]
        })
        codes = [flag["code"] for flag in report["scenes"][0]["flags"]]
        self.assertIn("artifact_not_retained_for_postcheck", codes)
        self.assertEqual(report["critical_flag_count"], 0)

    def test_existing_unreadable_artifact_is_critical_in_report_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.jpg")
            with open(path, "wb") as fh:
                fh.write(b"not-an-image")
            report = build_visual_quality_shadow_report({
                "scene_visuals": [{"scene_number": 1, "image_path": path, "final_visual_duration_sec": 4.0}]
            })
        self.assertEqual(report["critical_flag_count"], 1)
        self.assertFalse(report["blocking"])


class VisualQualityShadowPatchTests(unittest.TestCase):
    def test_patch_keeps_original_pipeline_result_and_adds_shadow_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, "scene.jpg")
            Image.new("RGB", (1280, 720), color=(120, 100, 80)).save(image_path)

            class FakeGenerator:
                def __init__(self):
                    self.ai_service = None
                    self.calls = 0

                def _ensure_image_for_scene(self, *args, **kwargs):
                    return image_path

                def create_video_from_plan(self, plan, *args, **kwargs):
                    self.calls += 1
                    path = self._ensure_image_for_scene("prompt", "fallback")
                    return {
                        "video_url": "/static/videos/ok.mp4",
                        "sentinel": "original-result",
                        "render_report": {
                            "scene_visuals": [{
                                "scene_number": 1,
                                "image_path": path,
                                "source": "generated_group",
                                "final_visual_duration_sec": 5.0,
                                "max_visual_hold_sec": 5.0,
                            }]
                        },
                    }

            install_visual_quality_shadow_patch(FakeGenerator)
            install_visual_quality_shadow_patch(FakeGenerator)
            generator = FakeGenerator()
            with patch.dict(os.environ, {"ENABLE_VISUAL_QA_SHADOW": "true"}, clear=False):
                result = generator.create_video_from_plan({"title": "Teste"})

        self.assertEqual(generator.calls, 1)
        self.assertEqual(result["sentinel"], "original-result")
        self.assertEqual(result["video_url"], "/static/videos/ok.mp4")
        self.assertIn("visual_quality_shadow", result)
        self.assertEqual(result["visual_quality_shadow"]["paid_ai_calls"], 0)

    def test_feature_flag_off_returns_original_result_without_shadow(self):
        class FakeGenerator:
            def __init__(self):
                self.ai_service = None

            def _ensure_image_for_scene(self, *args, **kwargs):
                return None

            def create_video_from_plan(self, plan, *args, **kwargs):
                return {"sentinel": "unchanged", "render_report": {}}

        install_visual_quality_shadow_patch(FakeGenerator)
        with patch.dict(os.environ, {"ENABLE_VISUAL_QA_SHADOW": "false"}, clear=False):
            result = FakeGenerator().create_video_from_plan({})
        self.assertEqual(result, {"sentinel": "unchanged", "render_report": {}})

    def test_shadow_failure_is_fail_open(self):
        class FakeGenerator:
            def __init__(self):
                self.ai_service = None

            def _ensure_image_for_scene(self, *args, **kwargs):
                return None

            def create_video_from_plan(self, plan, *args, **kwargs):
                return {"sentinel": "still-ok", "render_report": {"scene_visuals": []}}

        install_visual_quality_shadow_patch(FakeGenerator)
        # Um captured_images inválido não deve impedir a produção; o wrapper
        # contém qualquer falha interna e devolve o resultado canônico.
        instance = FakeGenerator()
        instance._codexia_visual_shadow_images = "invalid"
        with patch.dict(os.environ, {"ENABLE_VISUAL_QA_SHADOW": "true"}, clear=False):
            result = instance.create_video_from_plan({})
        self.assertEqual(result["sentinel"], "still-ok")
        self.assertIn("visual_quality_shadow", result)


if __name__ == "__main__":
    unittest.main()
