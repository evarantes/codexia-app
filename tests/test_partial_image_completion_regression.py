from pathlib import Path


def test_retry_does_not_turn_incomplete_images_into_render_only():
    source = Path("app/routers/youtube.py").read_text(encoding="utf-8")

    assert "missing_images == 0" in source
    assert '"max_new_image_calls": missing_images' in source
    assert '"force_render_only": False' in source
    assert '"repair_image_budget": budget' in source


def test_partial_recovery_generates_groups_after_preserved_images():
    source = Path("app/services/video_generator.py").read_text(encoding="utf-8")

    assert "partial_image_recovery = recovery_image_budget.enabled" in source
    assert "selected_image_count=(0 if partial_image_recovery else len(selected_image_paths))" in source
    assert "visual_group_id < len(selected_image_paths)" in source


def test_unified_pipeline_replaces_stale_video_and_enforces_contract_target():
    source = Path("app/services/unified_video_pipeline.py").read_text(encoding="utf-8")

    assert "contract_expected_images" in source
    assert "max(int(uv.image_count or 1), contract_expected_images)" in source
    assert "uv.video_path = normalized_video_path" in source
    assert "uv.video_duration_seconds = None" in source
