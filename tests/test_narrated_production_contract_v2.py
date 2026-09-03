from __future__ import annotations

from pathlib import Path

import pytest

from app.services.narrated_production_contract import (
    apply_approved_audio_as_source_of_truth,
    build_narration_review_state,
    preserve_approved_audio,
    validate_spoken_text,
)


def test_rejects_technical_text_before_tts():
    with pytest.raises(ValueError):
        validate_spoken_text('CENA 1 prompt: {"text": "Jesus é justo"}')


def test_review_state_waits_for_human_approval():
    state = build_narration_review_state(
        spoken_text="Jesus nos chama a viver a justiça com verdade e misericórdia.",
        audio_url="/media/narration-v1.mp3",
        duration_seconds=18.4,
    )
    assert state["status"] == "awaiting_narration_review"
    assert state["approved"] is False
    assert state["next_action"] == "approve_or_rebuild_narration"


def test_approved_mp3_is_copied_to_task_and_becomes_duration_truth(tmp_path: Path):
    source = tmp_path / "preview.mp3"
    source.write_bytes(b"ID3" + b"audio" * 100)
    task_dir = tmp_path / "task-123"

    approved = preserve_approved_audio(
        source_path=str(source),
        task_dir=str(task_dir),
        spoken_text="Jesus é o padrão de justiça que transforma nossas escolhas diárias.",
        duration_seconds=34.25,
    )
    source.unlink()

    assert Path(approved.path).exists()
    plan = apply_approved_audio_as_source_of_truth(
        {"duration_seconds": 15, "duration_source": "requested"},
        approved,
    )
    assert plan["seed_audio_path"] == approved.path
    assert plan["approved_audio_duration_seconds"] == 34.25
    assert plan["render_target_duration_seconds"] == 34.25
    assert plan["duration_source"] == "approved_audio"
    assert plan["tts_locked"] is True
    assert plan["allow_tts_generation"] is False


def test_audio_feedback_is_preserved_for_rebuild_request():
    state = build_narration_review_state(
        spoken_text="Uma narração humana e clara.",
        audio_url="/media/narration.mp3",
        duration_seconds=10.0,
        feedback="Fale mais devagar e com mais emoção no fechamento.",
        version=2,
    )
    assert "mais devagar" in state["feedback"]
    assert state["version"] == 2
