from scripts import apply_retry_plan_confirmation_stability as hardening


def test_retry_hash_validation_uses_persisted_task_state_only():
    source = f"""def retry():\n        {hardening.BACKEND_OLD}\n        return optimization_plan\n"""
    patched = hardening.patch_youtube(source)

    assert hardening.BACKEND_OLD not in patched
    assert "_intelligent_retry_visual_materials(task_id)" in patched
    assert "_intelligent_retry_visual_materials(task_id, payload)" not in patched
    assert hardening.patch_youtube(patched) == patched


def test_retry_ui_surfaces_structured_api_error_and_keeps_cancelled_task_for_diagnostics():
    source = "\n".join(
        [
            hardening.PLAN_ERROR_OLD,
            hardening.RETRY_ERROR_OLD,
            hardening.OPEN_RECOVERABLE_OLD,
        ]
    )
    patched = hardening.patch_index(source)

    assert "const planDetail = planData && planData.detail;" in patched
    assert "const retryDetail = data && data.detail;" in patched
    assert "detail.message || detail.code" not in patched  # names are planDetail/retryDetail
    assert "planDetail.message || planDetail.code" in patched
    assert "retryDetail.message || retryDetail.code" in patched
    assert "this.ytStoryTaskId = taskId;" in patched
    assert "await this.diagnoseStoryTask();" in patched
    assert hardening.MARKER in patched
    assert hardening.patch_index(patched) == patched


def test_no_paid_media_or_content_policy_is_changed_by_this_fix():
    script = open("scripts/apply_retry_plan_confirmation_stability.py", encoding="utf-8").read()

    assert "paid_image_calls" not in script
    assert "paid_tts_calls" not in script
    assert "story_content" not in script
    assert "seeded_script" not in script
    assert "selected_images" not in script
    assert "reuse_audio_from" not in script
