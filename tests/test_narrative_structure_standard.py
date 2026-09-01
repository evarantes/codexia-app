import json

from app.services.narrative_structure_standard import (
    NARRATIVE_STRUCTURE_STANDARD_VERSION,
    PROFILE_BIBLICAL_STORY,
    PROFILE_BIBLE_EXPLAINER,
    PROFILE_COMFORT_FAITH,
    PROFILE_DEVOTIONAL_EMOTIONAL,
    PROFILE_GENERAL_NARRATED,
    PROFILE_GUIDED_PRAYER,
    narrative_structure_prompt,
    select_narrative_profile,
)
from app.services.story_review_editor import generate_review_ready_story_text


def test_selects_profiles_by_kind_and_theme():
    assert select_narrative_profile("devotional", "Jesus, o motivo de eu existir") == PROFILE_DEVOTIONAL_EMOTIONAL
    assert select_narrative_profile("prayer", "oração da noite") == PROFILE_GUIDED_PRAYER
    assert select_narrative_profile("story", "Davi e Golias") == PROFILE_BIBLICAL_STORY
    assert select_narrative_profile("story", "uma história sobre perdão entre irmãos") == PROFILE_GENERAL_NARRATED
    assert select_narrative_profile("devotional", "o que significa nascer de novo?") == PROFILE_BIBLE_EXPLAINER
    assert select_narrative_profile("devotional", "Deus não esqueceu de você no sofrimento") == PROFILE_COMFORT_FAITH


def test_global_prompt_has_canonical_arc_and_no_fixed_duration():
    prompt = narrative_structure_prompt(
        PROFILE_DEVOTIONAL_EMOTIONAL,
        kind="devotional",
        instruction="Deus nunca se esqueceu de você",
    )
    lowered = prompt.lower()
    assert "escopo: global" in lowered
    assert "gancho" in lowered
    assert "identificação" in lowered
    assert "verdade bíblica" in lowered
    assert "transformação" in lowered
    assert "aplicação pessoal" in lowered
    assert "clímax" in lowered
    assert "reflexão final" in lowered
    assert "cta separado" in lowered
    assert "não force cinco minutos" in lowered
    assert "nunca coloque no texto narrável rótulos" in lowered


class DummyAIService:
    def __init__(self):
        self.prompt = ""
        self.system_prompt = ""
        self.legacy_called = False

    def _generate_text(self, prompt, system_prompt=None, json_mode=False):
        self.prompt = prompt
        self.system_prompt = system_prompt or ""
        assert json_mode is True
        return json.dumps({
            "title": "Jesus no centro da existência",
            "text": (
                "Existe uma pergunta que muda o modo como enxergamos cada escolha: por que estamos aqui? "
                "Quando as respostas parecem pequenas, a fé cristã aponta para uma verdade maior. "
                "Em Jesus encontramos direção, perdão e uma esperança que não depende de circunstâncias perfeitas. "
                "Essa verdade muda a maneira como atravessamos dias bons e difíceis. "
                "Hoje, colocar Cristo no centro significa escolher viver com propósito, amor e confiança. "
                "Por isso, quando a pergunta sobre o sentido da vida voltar, lembre-se de que sua existência pode ser vivida diante de Deus, com Jesus no centro."
            ),
            "closing_message": "Sua vida tem propósito quando Jesus ocupa o centro.",
            "endcard_cta_text": "Inscreva-se e acompanhe novas mensagens.",
        }, ensure_ascii=False)

    def generate_story_or_devotional_text(self, **kwargs):
        self.legacy_called = True
        return "Fallback não deveria ser usado."


def test_shared_editor_applies_global_standard_used_by_youtube_auto():
    ai = DummyAIService()
    result = generate_review_ready_story_text(
        ai,
        instruction="Jesus, o motivo de eu existir",
        kind="devotional",
        duration_min_minutes=5,
        duration_max_minutes=5,
    )

    assert ai.legacy_called is False
    assert "ESTRUTURA NARRATIVA CANÔNICA DO CODEXIA" in ai.prompt
    assert "ESCOPO: GLOBAL" in ai.prompt
    assert "DURAÇÃO: 5 a 5 minuto(s)" in ai.prompt
    assert "Nunca transforme rótulos estruturais" in ai.system_prompt

    assert result["narrative_structure_applied"] is True
    assert result["narrative_standard_version"] == NARRATIVE_STRUCTURE_STANDARD_VERSION
    assert result["narrative_profile"] == PROFILE_DEVOTIONAL_EMOTIONAL
    assert result["narrative_standard_scope"] == "global"
    assert result["narrative_standard_entrypoint"] == "youtube_auto_and_shared_editor"
    assert result["narrative_standard"]["reference_style"] == "jesus_o_motivo_de_eu_existir_without_copying"


def test_prayer_uses_guided_prayer_profile_in_shared_editor():
    ai = DummyAIService()
    result = generate_review_ready_story_text(
        ai,
        instruction="oração da noite para entregar as preocupações a Deus",
        kind="prayer",
        duration_min_minutes=3,
        duration_max_minutes=4,
    )
    assert result["narrative_profile"] == PROFILE_GUIDED_PRAYER
    assert "Oração guiada" in ai.prompt
