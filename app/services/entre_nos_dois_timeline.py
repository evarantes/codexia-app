"""Mede a duração das falas para preparar a futura sincronização audiovisual.

Não produz lip-sync, vídeo ou áudio. Só aceita MP3s aprovados pelo fluxo
TTS e informa cortes/tempos reais medidos pelo FFprobe.
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Callable, Optional

from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class DialogueTimelineError(ValueError):
    pass


def probe_audio_seconds(path: str) -> float:
    """Mede a duração de áudio com FFprobe; não aceita estimativa pelo tamanho."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode != 0:
        raise DialogueTimelineError("Falha ao verificar duração real do áudio.")
    try:
        streams = json.loads(result.stdout).get("streams", [])
        seconds = float(streams[0]["duration"])
    except (ValueError, IndexError, KeyError, TypeError):
        raise DialogueTimelineError("MP3 sem duração de áudio verificável.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise DialogueTimelineError("Duração inválida detectada no áudio.")
    return seconds


def build_measured_timeline(
    audio_result: dict,
    *,
    pause_seconds: float = 0.35,
    probe: Optional[Callable[[str], float]] = None,
) -> dict:
    """Alinha falas pela duração real, sem forçar um vídeo de 30 segundos.

    O resultado não é aprovação de mídia: ainda faltam escuta, verificação
    da identidade visual e lip-sync. Exige quatro arquivos distintos.
    """
    canonical = prepare_speech_manifest()
    if audio_result.get("program") != canonical["program"]:
        raise DialogueTimelineError("Programa divergente.")
    if audio_result.get("approval_status") != "awaiting_human_review":
        raise DialogueTimelineError("Áudios fora do estado previsto para revisão.")
    project_id = audio_result.get("project_id")
    if not isinstance(project_id, int) or project_id <= 0:
        raise DialogueTimelineError("Projeto inválido.")
    if not math.isfinite(pause_seconds) or not (0 <= pause_seconds <= 3):
        raise DialogueTimelineError("Pausa fora do intervalo seguro.")

    provided = audio_result.get("segments")
    planned = canonical["segments"]
    if not isinstance(provided, list) or len(provided) != len(planned):
        raise DialogueTimelineError("Número de trechos de áudio divergente.")
    probe = probe or probe_audio_seconds
    elapsed = 0.0
    timeline = []
    seen_paths = set()
    for expected, audio in zip(planned, provided):
        if audio.get("index") != expected["index"] or audio.get("speaker") != expected["speaker"]:
            raise DialogueTimelineError("Ordem ou personagem divergente.")
        path = audio.get("path")
        if not isinstance(path, str) or not path.strip():
            raise DialogueTimelineError("Arquivo de áudio ausente.")
        resolved = Path(path).resolve()
        if resolved in seen_paths or not resolved.is_file() or resolved.suffix.lower() != ".mp3":
            raise DialogueTimelineError("Arquivo ausente, repetido ou com formato incorreto.")
        seen_paths.add(resolved)
        try:
            duration = float(probe(str(resolved)))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise DialogueTimelineError("Áudio não pôde ser medido.") from exc
        if not math.isfinite(duration) or not (0.2 <= duration <= 45.0):
            raise DialogueTimelineError("Duração de fala fora do intervalo seguro.")
        timeline.append({
            "index": expected["index"],
            "speaker": expected["speaker"],
            "voice_id": expected["voice_id"],
            "text": expected["text"],
            "audio_path": str(resolved),
            "start_seconds": round(elapsed, 3),
            "end_seconds": round(elapsed + duration, 3),
            "measured_duration_seconds": round(duration, 3),
            "requires_lip_sync": True,
        })
        elapsed += duration
        if expected["index"] != len(planned):
            elapsed += pause_seconds
    return {
        "program": canonical["program"],
        "project_id": project_id,
        "planned_seconds": canonical["target_seconds"],
        "measured_total_seconds": round(elapsed, 3),
        "segments": timeline,
        "caption_max_lines": 2,
        "status": "awaiting_audio_and_visual_review",
        "auto_publish": False,
        "video_generated": False,
    }
