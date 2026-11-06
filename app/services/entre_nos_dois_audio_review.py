"""Private per-user pilot speech segment uploads and playback.

This module does not synthesize audio. It imports four already-authorized MP3
files into the Studio's durable private directory for audible script review.
No public asset URLs, provider API calls, or automatic publication.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

from mutagen.mp3 import MP3, HeaderNotFoundError

from app.services.entre_nos_dois_references import StudioReferenceError, _storage_root
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MIN_DURATION = 0.2
MAX_DURATION = 45.0


class StudioAudioError(ValueError):
    pass


def _segment(index: int) -> dict:
    if type(index) is not int:
        raise StudioAudioError("Índice do trecho inválido.")
    manifest = prepare_speech_manifest()
    if not 1 <= index <= len(manifest["segments"]):
        raise StudioAudioError("Trecho fora do roteiro aprovado.")
    return manifest["segments"][index - 1]


def audio_file(*, user_id: int, index: int) -> Path:
    if type(user_id) is not int or user_id <= 0:
        raise StudioAudioError("Usuário inválido.")
    _segment(index)
    return _storage_root() / f"user-{user_id}" / "entre-nos-dois" / "audio" / f"segment-{index:02d}.mp3"


def _duration_seconds(body: bytes) -> float:
    try:
        audio = MP3(io.BytesIO(body))
        seconds = float(audio.info.length)
    except (HeaderNotFoundError, ValueError, OSError, TypeError) as exc:
        raise StudioAudioError("Arquivo MP3 inválido ou duração ilegível.") from None
    if not MIN_DURATION <= seconds <= MAX_DURATION:
        raise StudioAudioError("A duração de cada fala deve estar entre 0,2 e 45 segundos.")
    return round(seconds, 3)


def store_segment_audio(*, user_id: int, index: int, body: bytes) -> dict:
    planned = _segment(index)
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_AUDIO_BYTES:
        raise StudioAudioError("Áudio inválido ou acima de 10 MB.")
    seconds = _duration_seconds(body)
    try:
        path = audio_file(user_id=user_id, index=index)
    except StudioReferenceError as exc:
        raise StudioAudioError(str(exc)) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".uploading")
    try:
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    except FileExistsError:
        raise StudioAudioError("Outro upload deste trecho está em andamento.") from None
    return {
        "index": index,
        "speaker": planned["speaker"],
        "stored": True,
        "private": True,
        "duration_seconds": seconds,
        "review_status": "awaiting_human_review",
        "auto_publish": False,
    }


def list_segment_audio(*, user_id: int) -> dict:
    segments = []
    for segment in prepare_speech_manifest()["segments"]:
        index = segment["index"]
        try:
            path = audio_file(user_id=user_id, index=index)
        except StudioReferenceError as exc:
            raise StudioAudioError(str(exc)) from None
        exists = path.is_file()
        seconds = None
        if exists:
            try:
                seconds = _duration_seconds(path.read_bytes())
            except StudioAudioError:
                exists = False
        segments.append({
            "index": index,
            "speaker": segment["speaker"],
            "text": segment["text"],
            "uploaded": exists,
            "duration_seconds": seconds,
        })
    return {
        "segments": segments,
        "all_uploaded": all(segment["uploaded"] for segment in segments),
        "requires_human_review": True,
        "auto_publish": False,
    }


def preview_audio_timeline(*, user_id: int, pause_seconds: float = 0.35) -> dict:
    """Measured browser storyboard, no rendering, TTS, lip-sync or publishing.

    Incomplete audio never masquerades as a fully timed pilot.
    """
    if type(pause_seconds) not in (int, float) or not 0 <= pause_seconds <= 2:
        raise StudioAudioError("Pausa inválida.")
    data = list_segment_audio(user_id=user_id)
    segments = data["segments"]
    if not data["all_uploaded"]:
        return {
            "ready": False,
            "missing_indices": [x["index"] for x in segments if not x["uploaded"]],
            "segments": [],
            "total_seconds": None,
            "planned_seconds": 30,
            "auto_publish": False,
        }
    elapsed = 0.0
    scenes = []
    for segment in segments:
        duration = float(segment["duration_seconds"])
        scenes.append({
            "index": segment["index"],
            "speaker": segment["speaker"],
            "text": segment["text"],
            "start_seconds": round(elapsed, 3),
            "end_seconds": round(elapsed + duration, 3),
            "duration_seconds": duration,
        })
        elapsed += duration
        if segment["index"] != segments[-1]["index"]:
            elapsed += pause_seconds
    return {
        "ready": True,
        "missing_indices": [],
        "segments": scenes,
        "pause_seconds": pause_seconds,
        "total_seconds": round(elapsed, 3),
        "planned_seconds": 30,
        "requires_human_review": True,
        "is_rendered_video": False,
        "auto_publish": False,
    }
