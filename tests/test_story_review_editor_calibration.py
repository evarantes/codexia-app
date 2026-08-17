import json

from app.services.story_review_editor import (
    _editorial_quality_issues,
    generate_review_ready_story_text,
)


def test_quality_guard_flags_generic_ai_style():
    title = "Jesus: O Coração da Nossa Jornada"
    text = (
        "Em meio ao turbilhão da vida, buscamos uma âncora, uma bússola e um porto. "
        "A tempestade parece dominar os mares, mas uma luz rompe as trevas e mostra o caminho. "
        "No fim, Jesus continua sendo a resposta para a nossa vida."
    )
    issues = _editorial_quality_issues(title, text, "Jesus, quem Ele é na sua vida?")

    assert "generic_title" in issues
    assert "generic_opening" in issues
    assert "metaphor_stacking" in issues
    assert "weak_return_to_theme" not in issues


class _FakeAI:
    def __init__(self):
        self.calls = 0

    def _generate_text(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "title": "Jesus: O Coração da Nossa Jornada",
                    "text": (
                        "Em meio ao turbilhão da vida, buscamos uma âncora, uma bússola, um porto e uma luz. "
                        "A tempestade passa pelos mares e pelas trevas. No fim, Jesus segue perto de nós."
                    ),
                    "closing_message": "Jesus segue perto de você.",
                    "endcard_cta_text": "Inscreva-se e acompanhe novas mensagens.",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "title": "Quem é Jesus quando as respostas não bastam?",
                "text": (
                    "Há perguntas que não cabem em uma definição pronta. Quem é Jesus para nós quando as respostas fáceis deixam de funcionar? "
                    "Conhecê-lo não é apenas lembrar uma figura da história, mas reconhecer sua presença no modo como enfrentamos a culpa, a esperança e as escolhas de cada dia. "
                    "Por isso, a pergunta volta para o lugar certo: quem é Jesus na sua vida hoje? A resposta não precisa ser um slogan; precisa aparecer na forma como você decide caminhar com Ele."
                ),
                "closing_message": "Conhecer Jesus é permitir que sua presença transforme a vida de hoje.",
                "endcard_cta_text": "Inscreva-se e acompanhe novas mensagens.",
            },
            ensure_ascii=False,
        )

    def generate_story_or_devotional_text(self, **_kwargs):
        raise AssertionError("legacy fallback should not be used")


def test_structured_generation_retries_once_when_quality_guard_rejects_first_answer():
    ai = _FakeAI()

    result = generate_review_ready_story_text(
        ai,
        instruction="Jesus, quem Ele é na sua vida?",
        kind="story",
        duration_min_minutes=1,
        duration_max_minutes=1,
    )

    assert ai.calls == 2
    assert result["editorial_source"] == "structured_ai_retry"
    assert result["title"] == "Quem é Jesus quando as respostas não bastam?"
    assert result["editorial_quality_issues"] == []
