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
        image_count=8,
        script_json=json.dumps({"title": "Teste", "text": "Narração", "selected_images": [
            f"/data/media/images/scene-{index}.png" for index in range(8)
        ]}),
        storyboard_json=json.dumps({"scenes": [{"text": "Cena"}]}),
        images_json=json.dumps({"paths": [f"/data/media/images/scene-{index}.png" for index in range(8)]}),
        audio_path="/data/media/audio/narration.mp3",
        video_path="/data/media/videos/final.mp4",
        audio_size_bytes=None,
        audio_duration_seconds=None,
        video_size_bytes=None,
        video_duration_seconds=None,
        video_url=None,
    )


def _task_result(
    *,
    reused=0,
    average=8.0,
    caption_source="official_audio_transcript",
    unique_images=8,
    requested_seconds=None,
):
    images = [f"/data/media/images/scene-{index}.png" for index in range(unique_images)]
    scenes = [
        {
            "scene_number": index + 1,
            "image_path": images[index % len(images)] if images else "",
            "final_visual_duration_sec": 8.0,
        }
        for index in range(8)
    ]
    payload = {"editorial_reviewed": True, "editorial_review_ready": True}
    if requested_seconds is not None:
        payload["duration_seconds"] = requested_seconds
    return {
        "payload": payload,
        "script": {"title": "Teste", "text": "Narração", "selected_images": images},
        "selected_images": images,
        "render_report": {
            "visual_plan": {
                "requested_image_count": 8,
                "group_count": 8,
                "reused_image_count": reused,
                "average_image_duration_sec": average,
            },
            "scene_visuals": scenes,
            "sync_validation": {
                "captions_synced_with_audio": True,
                "timeline_source": caption_source,
            },
        },
    }


def _validate(video_duration, task_result, audio_duration=None):
    service = UnifiedVideoPipelineService()
    uv = _unified()
    uv.audio_duration_seconds = audio_duration
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
            "video_duration": video_duration,
            "audio_duration": audio_duration or 0,
        },
    ), patch(
        "app.services.unified_video_pipeline.absolute_path_for_audio",
        side_effect=lambda value: value,
    ), patch(
        "app.services.unified_video_pipeline.absolute_path_for_video",
        side_effect=lambda value: value,
    ):
        return service.validate_before_awaiting_review(db, uv.task_id, probe_http=False)


def test_director_gate_rejects_video_shorter_than_requested_minimum():
    validation = _validate(314.969, _task_result())
    assert validation.ok is False
    assert validation.checks["duration_matches_request"] is False
    assert validation.first_failed == "duration_matches_request"
    assert validation.details["mp4"]["requested_duration_seconds"] == 600.0


def test_director_gate_rejects_one_image_repeated_and_approximate_captions():
    validation = _validate(
        600.0,
        _task_result(
            reused=3,
            average=8.0,
            caption_source="text_fallback_from_measured_audio",
            unique_images=1,
        ),
    )
    assert validation.ok is False
    assert validation.checks["visual_variety_valid"] is False
    assert validation.checks["caption_sync_valid"] is False
    visual = validation.details["director_quality"]["visual_variety"]
    assert visual["unique_rendered_image_count"] == 1
    assert visual["minimum_unique_image_count"] >= 2


def test_director_gate_accepts_varied_render_even_when_some_paths_repeat():
    validation = _validate(600.0, _task_result(reused=3, average=8.0))
    assert validation.ok is True, validation.details
    assert validation.checks["visual_variety_valid"] is True
    visual = validation.details["director_quality"]["visual_variety"]
    assert visual["unique_rendered_image_count"] == 8
    assert visual["path_reuse_is_advisory"] is True


def test_requested_duration_is_a_minimum_so_longer_complete_video_passes():
    validation = _validate(65.0, _task_result(requested_seconds=30))
    assert validation.ok is True, validation.details
    assert validation.checks["duration_matches_request"] is True
    assert validation.details["mp4"]["requested_duration_seconds"] == 30.0




def test_video_may_exceed_requested_minimum_but_must_cover_saved_narration():
    validation = _validate(
        36.321,
        _task_result(requested_seconds=30),
        audio_duration=60.0,
    )
    assert validation.checks["duration_matches_request"] is True
    assert validation.checks["duration_covers_narration"] is False
    assert validation.first_failed == "duration_covers_narration"


def test_director_gate_accepts_word_boundaries_and_local_real_audio_alignment():
    for source in ("approved_edge_tts_word_boundaries", "local_audio_activity_alignment"):
        validation = _validate(600.0, _task_result(caption_source=source))
        assert validation.ok is True, (source, validation.details)
        assert validation.checks["caption_sync_valid"] is True
