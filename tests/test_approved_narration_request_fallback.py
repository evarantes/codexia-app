from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/apply_youtube_narration_gate.py"


def test_guard_uses_core_owned_storage_and_source():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'Path(_APPROVED_AUDIO_ROOT).resolve() / "youtube_narration_core_v1"' in text
    assert 'approved_source == "youtube_narration_core_v1_approved"' in text
    assert 'script["approved_narration_required"] = True' in text


def test_guard_remains_fail_closed_against_core_metadata():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'approved_meta.get("approved") is True' in text
    assert 'approved_story_hash == approved_text_hash' in text
    assert 'approved_meta.get("narration_core_version")' in text
    assert 'approved_meta.get("narration_core_namespace")' in text
    assert '"tts_regeneration_allowed": False' in text
