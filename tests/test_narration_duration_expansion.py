import re
from pathlib import Path

from app.services.video_generator import VideoGenerator
from app.services.narration_duration_feedback import (
    calibrated_body_duration_target,
    planning_duration_bounds,
)


class ExpandingAI:
    def _generate_text(self, prompt, **_kwargs):
        match = re.search(r"entre\s+(\d+)\s+e\s+(\d+)\s+palavras", str(prompt))
        target = int(match.group(1)) if match else 1400
        sentences = []
        cursor = 0
        while cursor < target:
            chunk = [f"conteudo{idx}" for idx in range(cursor, min(target, cursor + 12))]
            sentences.append(" ".join(chunk) + ".")
            cursor += len(chunk)
        return " ".join(sentences)


class NonExpandingAI:
    def _generate_text(self, _prompt, **_kwargs):
        return "Texto curto sem expansão suficiente."


class ShortThenExpandingAI:
    def __init__(self):
        self.calls = 0

    def _generate_text(self, prompt, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return "A primeira expansão ficou curta demais."
        return ExpandingAI()._generate_text(prompt, **kwargs)


def _short_scenes(count=48):
    return [
        {
            "text": f"Cena {idx} apresenta uma ideia curta para a mensagem.",
            "_tts_text": f"Cena {idx} apresenta uma ideia curta para a mensagem.",
            "_estimated_narration_sec": 4.0,
        }
        for idx in range(1, count + 1)
    ]


def test_preflight_expands_short_script_to_requested_ten_minutes(tmp_path: Path):
    generator = VideoGenerator(output_dir=str(tmp_path), ai_service=ExpandingAI())
    plan = {
        "kind": "devotional",
        "target_duration_min": 10,
        "review_feedback": "Produzir aproximadamente 10 minutos sem repetir imagens.",
    }

    narration = generator.prepare_final_narration_text(plan, _short_scenes())

    assert narration["estimated_total_duration_sec"] >= 570.0
    assert narration["word_count"] >= 1200
    assert len(narration["scene_texts"]) == 48
    assert any(
        item.get("duration_action") == "expanded_short_narration"
        for item in narration["planning_attempts"]
    )


def test_expansion_fails_closed_when_ai_returns_another_short_text(tmp_path: Path):
    generator = VideoGenerator(output_dir=str(tmp_path), ai_service=NonExpandingAI())
    result = generator._expand_body_text_to_fit(
        "Uma mensagem curta que não atende à duração solicitada.",
        _short_scenes(4),
        target_min_sec=560.0,
        kind="devotional",
    )

    assert result["used_ai"] is False
    assert result["reason"] == "ai_did_not_expand_enough"


def test_director_retries_once_when_its_first_expansion_is_still_short(tmp_path: Path):
    ai = ShortThenExpandingAI()
    generator = VideoGenerator(output_dir=str(tmp_path), ai_service=ai)
    original_body = " ".join(["reflexao" for _ in range(90)])

    result = generator._expand_body_text_to_fit(
        original_body,
        _short_scenes(8),
        target_min_sec=90.0,
        kind="devotional",
    )

    assert result["used_ai"] is True
    assert result["attempt_count"] == 2
    assert ai.calls == 2
    assert result["returned_words"] >= result["requested_words"] - 5


def test_queue_failure_message_hides_internal_validation_dump():
    source = Path("app/routers/youtube.py").read_text(encoding="utf-8")
    assert "O Claude Diretor bloqueou o vídeo porque a duração ficou em" in source
    assert "Reinicie para o roteiro e a narração" in source


def test_real_audio_is_replanned_before_any_short_video_render():
    source = Path("app/services/video_generator.py").read_text(encoding="utf-8")
    assert '"duration_action": "expanded_short_real_audio"' in source
    assert "calibrated_body_duration_target(" in source
    assert "expansion_word_range(" in source
    assert "MAX_REAL_AUDIO_NARRATION_ATTEMPTS = 5" in source
    assert "playback_rate_for_target(" in source
    assert '"strategy": "pitch_preserving_speech_slowdown"' in source
    assert "a narracao permaneceu menor que a duracao solicitada" in source
    assert "O video nao foi renderizado" in source


def test_exact_target_has_a_feasible_planning_band():
    lower, upper = planning_duration_bounds(60, 60, 60)

    assert lower == 58.8
    assert upper == 61.2
    assert lower <= upper


def test_short_tts_audio_calibrates_the_next_body_target_to_real_pace():
    target = calibrated_body_duration_target(
        estimated_body_sec=40,
        actual_audio_sec=54,
        fixed_audio_sec=12,
        desired_audio_sec=58.8,
    )

    assert target > 40
    assert round(target, 2) == 44.57
