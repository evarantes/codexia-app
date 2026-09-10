import json
from types import SimpleNamespace
from unittest.mock import patch

from app.services.unified_video_pipeline import UnifiedVideoPipelineService


class LogoOnlyPreReviewValidationTests:
    def test_logo_only_does_not_require_storyboard_scenes(self):
        service = UnifiedVideoPipelineService()
        logo_path = "/data/media/channel-logo.png"
        task_result = {
            "script": {
                "title": "Logo-only render",
                "text": "Narração aprovada.",
                "logo_only_visuals": True,
            },
            "selected_images": [logo_path],
        }
        uv = SimpleNamespace(
            task_id="task-logo-only",
            idempotency_key="ik-logo-only",
            source_module="story",
            image_count=8,
            script_json=None,
            storyboard_json=json.dumps({"scenes": []}),
            images_json=json.dumps({"paths": [logo_path]}),
            audio_path="/data/media/audio/narration.wav",
            video_path="/data/media/videos/final.mp4",
            audio_size_bytes=None,
            audio_duration_seconds=None,
            video_size_bytes=None,
            video_duration_seconds=None,
            video_url=None,
        )
        db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

        with patch.object(service, "_find_any", return_value=uv), patch(
            "app.services.unified_video_pipeline.get_task",
            return_value={"result": task_result},
        ), patch.object(service, "_file_exists_or_url", return_value=True), patch(
            "app.services.unified_video_pipeline._file_size_bytes",
            return_value=256 * 1024,
        ), patch(
            "app.services.unified_video_pipeline._ffprobe_streams",
            return_value={
                "has_video": True,
                "has_audio": True,
                "video_duration": 12.0,
            },
        ), patch(
            "app.services.unified_video_pipeline.absolute_path_for_audio",
            side_effect=lambda value: value,
        ), patch(
            "app.services.unified_video_pipeline.absolute_path_for_video",
            side_effect=lambda value: value,
        ):
            validation = service.validate_before_awaiting_review(
                db,
                "ik-logo-only",
                probe_local_paths=True,
                probe_http=False,
            )

        assert validation.ok, validation.details
        assert validation.checks["storyboard_valid"]
        assert validation.details["storyboard"]["scene_count"] == 0
        assert validation.details["storyboard"]["logo_only_visuals"] is True
        assert validation.details["storyboard"]["scene_requirement"] == "not_required_logo_only"
        assert validation.details["images"]["expected_min"] == 1
        assert validation.details["images"]["validation_policy"] == "logo_only_single_asset"
        assert validation.details["images"]["actual_found"] == 1

    def test_normal_render_still_requires_storyboard_scenes(self):
        service = UnifiedVideoPipelineService()
        uv = SimpleNamespace(
            task_id="task-normal",
            idempotency_key="ik-normal",
            source_module="story",
            image_count=1,
            script_json=None,
            storyboard_json=json.dumps({"scenes": []}),
            images_json=json.dumps({"paths": ["/data/media/scene.png"]}),
            audio_path="/data/media/audio/narration.wav",
            video_path="/data/media/videos/final.mp4",
            audio_size_bytes=None,
            audio_duration_seconds=None,
            video_size_bytes=None,
            video_duration_seconds=None,
            video_url=None,
        )
        db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

        with patch.object(service, "_find_any", return_value=uv), patch(
            "app.services.unified_video_pipeline.get_task",
            return_value={
                "result": {
                    "script": {"title": "Normal render", "text": "Narração."},
                    "selected_images": ["/data/media/scene.png"],
                }
            },
        ), patch.object(service, "_file_exists_or_url", return_value=True), patch(
            "app.services.unified_video_pipeline._file_size_bytes",
            return_value=256 * 1024,
        ), patch(
            "app.services.unified_video_pipeline._ffprobe_streams",
            return_value={
                "has_video": True,
                "has_audio": True,
                "video_duration": 12.0,
            },
        ), patch(
            "app.services.unified_video_pipeline.absolute_path_for_audio",
            side_effect=lambda value: value,
        ), patch(
            "app.services.unified_video_pipeline.absolute_path_for_video",
            side_effect=lambda value: value,
        ):
            validation = service.validate_before_awaiting_review(
                db,
                "ik-normal",
                probe_local_paths=True,
                probe_http=False,
            )

        assert not validation.ok
        assert validation.first_failed == "storyboard_valid"
        assert validation.details["storyboard"]["logo_only_visuals"] is False


if __name__ == "__main__":
    import unittest

    unittest.main()
