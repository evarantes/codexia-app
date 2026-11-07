"""Planejamento de TTS separado por personagem, sem executar chamadas pagas.

A saída é um manifesto para o worker; uma integração real deve ler as
credenciais privadas do projeto e confirmar orçamento antes de executar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.services.entre_nos_dois import HOSTS, TEST_DIALOGUE, DialogueTurn, validate_dialogue


VOICE_PROVIDER = "elevenlabs"
# Limite operacional inicial: estimativa de caracteres, NÃO crédito real do provedor.
MAX_TEST_CHARACTERS = 1500


@dataclass(frozen=True)
class SpeechSegment:
    index: int
    speaker: str
    voice_id: str
    text: str
    target_seconds: int


def prepare_speech_manifest(
    dialogue: Sequence[DialogueTurn] = TEST_DIALOGUE,
    *,
    target_seconds: int = 30,
    max_characters: int = MAX_TEST_CHARACTERS,
) -> dict:
    """Prepara falas separadas, sem enviar texto nem dados a terceiros.

    Rejeita vozes não configuradas e não aproxima custo monetário:
    fornecedores têm preços diferentes e a duração precisa ser medida
    após TTS. A identidade visual é tratada por outro serviço.
    """
    dialogue = tuple(dialogue)
    validate_dialogue(dialogue, target_seconds=target_seconds)
    if max_characters <= 0:
        raise ValueError("Limite de caracteres inválido.")
    voices = {host.name: host.provider_voice_id for host in HOSTS}
    if any(not voice_id for voice_id in voices.values()):
        raise ValueError("Voz oficial não cadastrada para todos os apresentadores.")
    total_characters = sum(len(turn.text) for turn in dialogue)
    if total_characters > max_characters:
        raise ValueError("Teste excede o limite autorizado de caracteres.")
    segments = [
        SpeechSegment(
            index=index,
            speaker=turn.speaker,
            voice_id=voices[turn.speaker],
            text=turn.text,
            target_seconds=turn.target_seconds,
        )
        for index, turn in enumerate(dialogue, start=1)
    ]
    return {
        "program": "entre-nos-dois",
        "provider": VOICE_PROVIDER,
        "locale": "pt-BR",
        "approved_voices": dict(voices),
        "target_seconds": target_seconds,
        "estimated_characters": total_characters,
        "max_characters": max_characters,
        "requires_explicit_cost_approval": True,
        "requires_human_audio_review": True,
        "auto_publish": False,
        "status": "ready_for_provider_adapter",
        "segments": [
            {
                "index": segment.index,
                "speaker": segment.speaker,
                "voice_id": segment.voice_id,
                "text": segment.text,
                "target_seconds": segment.target_seconds,
                "audio_path": None,
                "measured_duration_seconds": None,
            }
            for segment in segments
        ],
    }
