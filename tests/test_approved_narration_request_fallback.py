from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "app" / "routers" / "youtube.py"


def test_guard_uses_core_owned_storage_and_source():
    text = ROUTER.read_text(encoding="utf-8")
    assert "production_job_store.validated_approved_audio" in text
    assert '"source": "youtube_narration_core_v1_approved"' in text
    assert 'script["seed_audio_path"] = approved_narration_contract["render_audio_path"]' in text
    assert 'script["approved_narration_required"] = True' in text


def test_guard_remains_fail_closed_against_core_metadata():
    text = ROUTER.read_text(encoding="utf-8")
    assert "text_sha256 != computed_text_sha256" in text
    assert "core_version != NARRATION_CORE_VERSION" in text
    assert "core_namespace != NARRATION_CORE_NAMESPACE" in text
    assert 'script["allow_tts_generation"] = False' in text
    assert '"tts_regeneration_allowed": False' in text
