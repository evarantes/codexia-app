import re
from pathlib import Path

from app.services.video_generator import VideoGenerator


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


def test_queue_failure_message_hides_internal_validation_dump():
    source = Path("app/routers/youtube.py").read_text(encoding="utf-8")
    assert "O Claude Diretor bloqueou o vídeo porque a duração ficou em" in source
    assert "Reinicie para o roteiro e a narração" in source


def test_real_audio_is_replanned_before_any_short_video_render():
    source = Path("app/services/video_generator.py").read_text(encoding="utf-8")
    assert '"duration_action": "expanded_short_real_audio"' in source
    assert "a narracao permaneceu menor que a duracao solicitada" in source
    assert "O video nao foi renderizado" in source
