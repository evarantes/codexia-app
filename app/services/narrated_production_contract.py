from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, asdict
from typing import Any, Dict, Mapping, Optional


NARRATED_PRODUCTION_CONTRACT_VERSION = 2
TECHNICAL_TOKENS = (
    "```", "{", "}", "[scene", "scene_", "prompt:", "camera:", "metadata:",
    "json", "python", "javascript", "http://", "https://", "/data/", "/app/",
)


@dataclass(frozen=True)
class ApprovedNarration:
    path: str
    sha256: str
    duration_seconds: float
    text: str
    approved: bool = True
    contract_version: int = NARRATED_PRODUCTION_CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_spoken_text(text: Any) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if not value:
        raise ValueError("A narração não pode estar vazia.")
    folded = value.lower()
    suspicious = [token for token in TECHNICAL_TOKENS if token in folded]
    if suspicious:
        raise ValueError(
            "Texto técnico detectado antes do TTS: " + ", ".join(suspicious[:5])
        )
    if re.search(r"(?i)\b(scene|prompt|metadata|camera|json)\s*[=:]", value):
        raise ValueError("Texto técnico detectado antes do TTS.")
    return value


def preserve_approved_audio(
    *,
    source_path: str,
    task_dir: str,
    spoken_text: str,
    duration_seconds: float,
    filename: str = "approved_narration.mp3",
) -> ApprovedNarration:
    """Copia o MP3 aprovado para armazenamento durável da própria produção.

    A partir desta cópia o renderer deve consumir somente este arquivo. O cache/origem
    deixa de ser dependência da produção.
    """
    spoken = validate_spoken_text(spoken_text)
    src = os.path.abspath(str(source_path or ""))
    if not src or not os.path.isfile(src) or os.path.getsize(src) <= 0:
        raise FileNotFoundError("MP3 da narração aprovada não está disponível.")
    dst_dir = os.path.abspath(str(task_dir or ""))
    if not dst_dir:
        raise ValueError("Diretório durável da tarefa não informado.")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, filename)
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    if not os.path.isfile(dst) or os.path.getsize(dst) <= 0:
        raise IOError("Falha ao preservar o MP3 aprovado na produção.")
    return ApprovedNarration(
        path=dst,
        sha256=sha256_file(dst),
        duration_seconds=max(0.0, float(duration_seconds or 0.0)),
        text=spoken,
    )


def validate_approved_narration(payload: Mapping[str, Any] | None) -> ApprovedNarration:
    data = dict(payload or {})
    if not bool(data.get("approved", True)):
        raise ValueError("A narração ainda não foi aprovada.")
    path = os.path.abspath(str(data.get("path") or ""))
    if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise FileNotFoundError("MP3 aprovado não encontrado no armazenamento da produção.")
    expected = str(data.get("sha256") or "").strip().lower()
    actual = sha256_file(path)
    if not expected or expected != actual:
        raise ValueError("Integridade do MP3 aprovado inválida; o render foi bloqueado.")
    spoken = validate_spoken_text(data.get("text"))
    duration = max(0.0, float(data.get("duration_seconds") or 0.0))
    if duration <= 0:
        raise ValueError("Duração real do MP3 aprovado não foi registrada.")
    return ApprovedNarration(path=path, sha256=actual, duration_seconds=duration, text=spoken)


def apply_approved_audio_as_source_of_truth(
    plan: Mapping[str, Any] | None,
    approved: Mapping[str, Any] | ApprovedNarration,
) -> Dict[str, Any]:
    """Fixa MP3 aprovado e sua duração como fonte de verdade do render.

    O alvo solicitado continua útil para roteirização. Depois da aprovação humana,
    a duração física do MP3 manda no vídeo; o renderer não pode chamar TTS novamente.
    """
    result = dict(plan or {})
    narration = approved if isinstance(approved, ApprovedNarration) else validate_approved_narration(approved)
    if isinstance(narration, ApprovedNarration):
        validated = narration
        if not os.path.isfile(validated.path):
            validated = validate_approved_narration(validated.to_dict())
    else:  # pragma: no cover
        validated = narration
    result["approved_narration"] = validated.to_dict()
    result["seed_audio_path"] = validated.path
    result["official_audio_path"] = validated.path
    result["approved_audio_sha256"] = validated.sha256
    result["approved_audio_duration_seconds"] = validated.duration_seconds
    result["render_target_duration_seconds"] = validated.duration_seconds
    result["duration_source"] = "approved_audio"
    result["tts_locked"] = True
    result["allow_tts_generation"] = False
    result["approved_narration_required"] = True
    return result


def build_narration_review_state(
    *,
    spoken_text: str,
    audio_url: str,
    duration_seconds: float,
    approved: bool = False,
    feedback: str = "",
    version: int = 1,
) -> Dict[str, Any]:
    """Contrato de UI compartilhado por YouTube Auto, séries e demais produtores."""
    return {
        "contract_version": NARRATED_PRODUCTION_CONTRACT_VERSION,
        "status": "approved" if approved else "awaiting_narration_review",
        "spoken_text": validate_spoken_text(spoken_text),
        "audio_url": str(audio_url or "").strip(),
        "duration_seconds": max(0.0, float(duration_seconds or 0.0)),
        "approved": bool(approved),
        "feedback": str(feedback or "").strip(),
        "version": max(1, int(version or 1)),
        "next_action": "generate_visuals_and_render" if approved else "approve_or_rebuild_narration",
    }


__all__ = [
    "NARRATED_PRODUCTION_CONTRACT_VERSION",
    "ApprovedNarration",
    "validate_spoken_text",
    "preserve_approved_audio",
    "validate_approved_narration",
    "apply_approved_audio_as_source_of_truth",
    "build_narration_review_state",
]
