"""Contrato editorial e visual para o programa Entre Nós Dois.

Este módulo é deliberadamente independente do pipeline de renderização legado.
Imagens de referência e identificadores de voz devem ficar em storage privado,
nunca em commits de um repositório público.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


PROGRAM_SLUG = "entre-nos-dois"
PROGRAM_TITLE = "Entre Nós Dois — com Gabriel e Helena"
PROGRAM_DISCLOSURE = "Gabriel e Helena são apresentadores virtuais criados com inteligência artificial."
PILOT_TITLE = "Quem deve pedir desculpas primeiro no casamento?"


@dataclass(frozen=True)
class VirtualHost:
    name: str
    personality: str
    voice_style: str
    # Chaves de ativos por projeto, não URLs públicas nem dados biométricos.
    private_reference_key: Optional[str] = None
    provider_voice_id: Optional[str] = None


GABRIEL = VirtualHost(
    name="Gabriel",
    personality="Acolhedor, sensato, bem-humorado e capaz de reconhecer erros.",
    voice_style="pt-BR; masculino; médio-grave; natural; pausas e respiração.",
)
HELENA = VirtualHost(
    name="Helena",
    personality="Perspicaz, afetuosa, espontânea, firme e respeitosa.",
    voice_style="pt-BR; feminino; expressivo; caloroso; pausas e respiração.",
)
HOSTS: Tuple[VirtualHost, ...] = (GABRIEL, HELENA)


@dataclass(frozen=True)
class DialogueTurn:
    speaker: str
    text: str
    target_seconds: int


# Somente prévia de roteiro; não representa áudio ou vídeo gerado.
TEST_DIALOGUE: Tuple[DialogueTurn, ...] = (
    DialogueTurn("Helena", "Gabriel, quando um casal briga, quem deve pedir desculpas primeiro?", 7),
    DialogueTurn("Gabriel", "Será que importa quem começou? O importante é reconhecer o erro e ouvir o outro.", 9),
    DialogueTurn("Helena", "Mas pedir desculpas não é fingir que nada aconteceu. É começar uma conversa sincera.", 9),
    DialogueTurn("Gabriel", "E você, daria o primeiro passo? Conta pra gente nos comentários!", 5),
)


def validate_dialogue(dialogue: Tuple[DialogueTurn, ...] = TEST_DIALOGUE, *, target_seconds: int = 30) -> None:
    """Valida o plano do teste, sem prometer duração real de TTS."""
    if not dialogue:
        raise ValueError("É necessário incluir falas.")
    if target_seconds <= 0:
        raise ValueError("Duração pretendida inválida.")
    if sum(turn.target_seconds for turn in dialogue) != target_seconds:
        raise ValueError("A soma das durações planejadas deve corresponder ao alvo.")
    for turn in dialogue:
        if turn.speaker not in {"Gabriel", "Helena"}:
            raise ValueError("Apresentador desconhecido.")
        if not turn.text.strip() or turn.target_seconds <= 0:
            raise ValueError("Fala inválida.")


def build_test_plan(*, project_id: int, reference_keys: dict[str, str],
                    voice_ids: dict[str, str]) -> dict:
    """Cria plano para revisão humana; não chama APIs nem publica conteúdo."""
    if project_id <= 0:
        raise ValueError("Projeto inválido.")
    validate_dialogue()
    if any(not reference_keys.get(h.name) for h in HOSTS):
        raise ValueError("Cadastre as referências visuais privadas de ambos os personagens.")
    if any(not voice_ids.get(h.name) for h in HOSTS):
        raise ValueError("Aprove uma voz para cada personagem.")
    if any(not reference_keys[h.name].startswith(f"projects/{project_id}/") for h in HOSTS):
        raise ValueError("Referência visual fora do projeto.")
    if reference_keys["Gabriel"] == reference_keys["Helena"]:
        raise ValueError("Cada personagem precisa de referência visual própria.")
    return {
        "program": PROGRAM_SLUG,
        "project_id": project_id,
        "title": "Teste de consistência — Gabriel e Helena",
        "kind": "virtual_couple_dialogue",
        "language": "pt-BR",
        "target_duration_seconds": 30,
        "disclosure": PROGRAM_DISCLOSURE,
        "caption_max_lines": 2,
        "requires_human_approval": True,
        "auto_publish": False,
        "reference_keys": {h.name: reference_keys[h.name] for h in HOSTS},
        "voice_ids": {h.name: voice_ids[h.name] for h in HOSTS},
        "dialogue": [
            {"speaker": turn.speaker, "text": turn.text, "target_seconds": turn.target_seconds}
            for turn in TEST_DIALOGUE
        ],
        "production_status": "awaiting_render_integration",
    }
