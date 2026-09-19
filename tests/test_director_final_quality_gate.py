import json
from types import SimpleNamespace
from unittest.mock import patch

from app.services.unified_video_pipeline import UnifiedVideoPipelineService


def _unified(duration_minutes=10):
    return SimpleNamespace(
        task_id="director-task",
        idempotency_key="director-quality-gate",
        source_module="story",
        duration_minutes=duration_minutes,
        image_count=1,
        script_json=json.dumps({"title": "Teste", "text": "Narração"}),
        storyboard_json=json.dumps({"scenes": [{"text": "Cena"}]}),
        images_json=json.dumps({"paths": ["/data/media/images/scene.png"]}),
        audio_path="/data/media/audio/narration.mp3",
        video_path="/data/media/videos/final.mp4",
        audio_size_bytes=None,
        audio_duration_seconds=None,
        video_size_bytes=None,
        video_duration_seconds=None,
        video_url=None,
    )


def _task_result(*, reused=0, average=25.0, caption_source="official_audio_transcript"):
    return {
        "payload": {"editorial_reviewed": True, "editorial_review_ready": True},
        "script": {"title": "Teste", "text": "Narração"},
        "selected_images": ["/data/media/images/scene.png"],
        "render_report": {
            "visual_plan": {
                "reused_image_count": reused,
                "average_image_duration_sec": average,
            },
            "sync_validation": {
                "captions_synced_with_audio": True,
                "timeline_source": caption_source,
            },
        },
    }


def _validate(video_duration, task_result):
    service = UnifiedVideoPipelineService()
    uv = _unified()
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    with patch.object(service, "_find_any", return_value=uv), patch(
        "app.services.unified_video_pipeline.get_task",
        return_value={"result": task_result},
    ), patch.object(service, "_file_exists_or_url", return_value=True), patch(
        "app.services.unified_video_pipeline._file_size_bytes",
        return_value=256 * 1024,
    ), patch(
        "app.services.unified_video_pipeline._ffprobe_streams",
        return_value={"has_video": True, "has_audio": True, "video_duration": video_duration},
    ), patch(
        "app.services.unified_video_pipeline.absolute_path_for_audio",
        side_effect=lambda value: value,
    ), patch(
        "app.services.unified_video_pipeline.absolute_path_for_video",
        side_effect=lambda value: value,
    ):
        return service.validate_before_awaiting_review(db, uv.task_id, probe_http=False)


def test_director_gate_rejects_five_minute_video_requested_as_ten_minutes():
    validation = _validate(314.969, _task_result())
    assert validation.ok is False
    assert validation.checks["duration_matches_request"] is False
    assert validation.first_failed == "duration_matches_request"
    assert validation.details["mp4"]["requested_duration_seconds"] == 600.0


def test_director_gate_rejects_repeated_visuals_and_approximate_captions():
    validation = _validate(
        600.0,
        _task_result(
            reused=3,
            average=42.0,
            caption_source="text_fallback_from_measured_audio",
        ),
    )
    assert validation.ok is False
    assert validation.checks["visual_variety_valid"] is False
    assert validation.checks["caption_sync_valid"] is False


def test_director_gate_accepts_requested_duration_unique_visuals_and_audio_timestamps():
    validation = _validate(598.0, _task_result())
    assert validation.ok is True, validation.details
    assert validation.checks["duration_matches_request"] is True
    assert validation.checks["visual_variety_valid"] is True
    assert validation.checks["caption_sync_valid"] is True


def test_director_gate_accepts_word_boundaries_and_local_real_audio_alignment():
    for source in ("approved_edge_tts_word_boundaries", "local_audio_activity_alignment"):
        validation = _validate(600.0, _task_result(caption_source=source))
        assert validation.ok is True, (source, validation.details)
        assert validation.checks["caption_sync_valid"] is True
