from pathlib import Path


def test_logo_only_mode_remains_zero_paid_visuals_during_narration_recovery():
    root = Path(__file__).resolve().parents[1]
    logo_service = (root / "app/services/logo_only_visual_mode.py").read_text(encoding="utf-8")
    assert 'normalized["image_count"] = 1' in logo_service
    assert 'normalized["disable_ai_image_generation"] = True' in logo_service
    assert 'normalized["disable_ai_thumbnail_generation"] = True' in logo_service


def test_structural_narration_recovery_does_not_enable_paid_recovery():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/apply_youtube_narration_gate.py").read_text(encoding="utf-8")
    assert 'tts_regeneration_allowed\": False' in script
    assert 'approved_narration_required\": True' in script
