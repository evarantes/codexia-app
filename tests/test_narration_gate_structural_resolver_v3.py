from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply_youtube_narration_gate.py"
ROUTER = ROOT / "app" / "routers" / "youtube.py"


def test_hardening_declares_structural_v3_resolver():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "narration-gate-approved-audio-reuse-v3" in text
    assert "CODEXIA_APPROVED_NARRATION_STRUCTURAL_RESOLVER_V1" in text
    assert 'task_payload.get("reuse_audio_from")' in text
    assert 'approved_root.glob(f"*/{approved_preview_id_hint}.json")' in text
    assert 'resolver\": \"structural_v3\"' in text


def test_applied_router_uses_gate_json_as_source_of_truth():
    text = ROUTER.read_text(encoding="utf-8")
    assert "narration-gate-approved-audio-reuse-v3" in text
    assert "CODEXIA_APPROVED_NARRATION_STRUCTURAL_RESOLVER_V1" in text
    assert 'get_task(task_id)' in text
    assert 'approved_meta_path' in text
    assert 'spoken_text_sent_to_tts' in text
    assert 'approved_narration_required' in text
    assert 'tts_regeneration_allowed' in text
    assert 'approved_source == "youtube_narration_gate_approved"' not in text


def test_v3_keeps_fail_closed_behavior():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "O vídeo foi bloqueado para impedir nova geração de TTS" in text
    assert "approved_story_hash == approved_text_hash" in text
    assert "_meta.get(\"approved\") is not True" in text
    assert "_mp3_path.stat().st_size <= 512" in text
