"""Adaptador isolado de TTS para o piloto Entre Nós Dois.

Não modifica o gerador legado. A chave é passada pelo worker em memória,
o usuário precisa autorizar o custo e NÃO há tentativas automáticas pagas.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib import error, request
from typing import Callable, Optional

from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest
from app.services.entre_nos_dois_voice_preflight import check_approved_voices

ELEVEN_API_ROOT = "https://api.elevenlabs.io/v1"
VOICE_MODEL = "eleven_multilingual_v2"
AUDIO_FORMAT = "mp3_44100_128"


class SpeechGenerationError(RuntimeError):
    pass


def _valid_mp3_header(audio: bytes) -> bool:
    """Cheap transport check; full decoding is required before approval."""
    return (
        isinstance(audio, bytes)
        and len(audio) > 1024
        and (audio.startswith(b"ID3") or audio[:2] in (bytes.fromhex("fffb"), bytes.fromhex("fff3"), bytes.fromhex("fff2")))
    )


def generate_pilot_audio(
    *,
    output_directory: str,
    api_key: str,
    approved_max_characters: int,
    project_id: int,
    manifest: Optional[dict] = None,
    post_audio: Optional[Callable[[str, dict, str], bytes]] = None,
    verify_voices: Optional[Callable[..., dict]] = None,
) -> dict:
    """Gera os quatro MP3s após aprovação explícita de consumo.

    post_audio permite mock dos testes. O limite é de caracteres e não
    equivale a um teto monetário; preço deve ser exibido e aprovado no
    frontend ANTES de chamar este método.
    """
    if project_id <= 0:
        raise ValueError("Projeto inválido")
    if not api_key or not api_key.strip():
        raise ValueError("Chave de API não configurada")
    if approved_max_characters <= 0:
        raise ValueError("Aprovação explícita do limite de caracteres é obrigatória")
    plan = manifest or prepare_speech_manifest()
    canonical = prepare_speech_manifest(max_characters=approved_max_characters)
    if plan.get("segments") != canonical["segments"] or plan.get("approved_voices") != canonical["approved_voices"]:
        raise ValueError("Roteiro ou vozes divergentes dos previamente aprovados")
    if plan.get("estimated_characters") != canonical["estimated_characters"]:
        raise ValueError("Contagem de caracteres inválida")
    if canonical["estimated_characters"] > approved_max_characters:
        raise ValueError("Limite autorizado insuficiente")

    # A única gravação permitida é abaixo do diretório reservado ao projeto.
    base = Path(output_directory).resolve()
    target_dir = base / "projects" / str(project_id) / "entre-nos-dois" / "audio"
    target_dir.mkdir(parents=True, exist_ok=True)
    if not target_dir.resolve().is_relative_to(base):
        raise ValueError("Destino fora do diretório autorizado")
    if post_audio is None:
        post_audio = _post_elevenlabs

    completed = []
    preflight_passed = False
    verify_voices = verify_voices or check_approved_voices
    for segment in canonical["segments"]:
        fingerprint = hashlib.sha256(
            (segment["voice_id"] + "\\n" + VOICE_MODEL + "\\n" + segment["text"]).encode("utf-8")
        ).hexdigest()[:16]
        name = f"{segment['index']:02d}_{segment['speaker'].lower()}_{fingerprint}.mp3"
        destination = target_dir / name
        # Evita cobranças repetidas na retomada. O validador do áudio real
        # (ffprobe/escuta) deve ocorrer antes da aprovação audiovisual.
        if destination.is_file():
            existing = destination.read_bytes()
            if _valid_mp3_header(existing):
                completed.append({"index": segment["index"], "speaker": segment["speaker"], "path": str(destination), "reused": True})
                continue
            raise SpeechGenerationError("Áudio em cache inválido; revisão manual obrigatória antes de repetir cobrança")
        if not preflight_passed:
            verification = verify_voices(api_key=api_key)
            if not verification.get("all_accessible") or len(verification.get("checked", [])) != len(canonical["approved_voices"]):
                raise SpeechGenerationError("Acesso às vozes não confirmado; nenhuma geração paga executada")
            verified_ids = {entry.get("speaker"): entry.get("voice_id") for entry in verification["checked"]}
            if verified_ids != canonical["approved_voices"]:
                raise SpeechGenerationError("IDs de voz divergentes; geração paga bloqueada")
            preflight_passed = True
        payload = {
            "text": segment["text"],
            "model_id": VOICE_MODEL,
        }
        url = f"{ELEVEN_API_ROOT}/text-to-speech/{segment['voice_id']}?output_format={AUDIO_FORMAT}"
        audio = post_audio(url, payload, api_key)
        if not _valid_mp3_header(audio):
            raise SpeechGenerationError(f"Áudio inválido no trecho {segment['index']}; não continuar com novas chamadas pagas")
        temp = destination.with_suffix(".part")
        try:
            temp.write_bytes(audio)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        completed.append({"index": segment["index"], "speaker": segment["speaker"], "path": str(destination), "reused": False})

    return {
        "program": "entre-nos-dois",
        "project_id": project_id,
        "segments": completed,
        "approved_characters": approved_max_characters,
        "character_count": canonical["estimated_characters"],
        "approval_status": "awaiting_human_review",
        "auto_publish": False,
    }


def _post_elevenlabs(url: str, payload: dict, api_key: str) -> bytes:
    req = request.Request(
        url,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with request.urlopen(req, timeout=90) as response:
            return response.read()
    except error.HTTPError as exc:
        # Não incluir corpo remoto ou credenciais no erro/log.
        raise SpeechGenerationError(f"Serviço de voz recusou a solicitação (HTTP {exc.code})") from None
    except (error.URLError, TimeoutError) as exc:
        raise SpeechGenerationError("Não foi possível confirmar a resposta do serviço de voz; confira o histórico antes de repetir") from None
