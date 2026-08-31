from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/apply_canonical_narration_logo_test_mode.py"


def test_guard_accepts_authenticated_top_level_fallback_metadata():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "CODEXIA_APPROVED_NARRATION_REQUEST_FALLBACK_V1" in text
    assert 'getattr(request, "approved_narration_preview_id", "")' in text
    assert 'getattr(request, "approved_narration_text_sha256", "")' in text


def test_guard_remains_fail_closed_against_gate_metadata():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'approved_meta.get("approved") is True' in text
    assert 'approved_story_hash == approved_text_hash' in text
    assert 'approved_source == "youtube_narration_gate_approved"' in text
