"""Scoped human review, bound to the existing MP4 rather than future renders."""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict


ASSET_CHECKS = {
    "script": {"script_valid", "storyboard_valid"},
    "images": {"image_count_minimum", "image_files_exist", "visual_variety_valid"},
    "narration": {"audio_exists_and_non_empty", "duration_matches_request", "duration_covers_narration"},
    "captions": {"caption_sync_valid"},
    "narration_caption_sync": {"caption_sync_valid"},
    "render": {"mp4_exists", "mp4_larger_than_100kb", "ffprobe_has_video_stream", "ffprobe_has_audio_stream", "duration_valid", "http_200_or_206"},
}
PHYSICAL_VIDEO_CHECKS = {"mp4_exists", "ffprobe_has_video_stream", "duration_valid"}
OVERRIDABLE_CHECKS = set().union(*ASSET_CHECKS.values()) - PHYSICAL_VIDEO_CHECKS
CHECK_HINTS = {
    "script_valid": "roteiro não encontrado ou incompleto",
    "storyboard_valid": "cenas não encontradas",
    "image_count_minimum": "quantidade de imagens abaixo do mínimo",
    "image_files_exist": "arquivos de imagens não encontrados",
    "visual_variety_valid": "variedade ou tempo contínuo das imagens precisa de revisão",
    "audio_exists_and_non_empty": "arquivo de narração não encontrado ou vazio",
    "duration_matches_request": "duração final abaixo da duração solicitada",
    "duration_covers_narration": "o vídeo termina antes da narração",
    "caption_sync_valid": "texto ou sincronização das legendas não comprovados",
    "mp4_exists": "arquivo MP4 não encontrado",
    "mp4_larger_than_100kb": "arquivo MP4 menor que o tamanho esperado",
    "ffprobe_has_video_stream": "faixa de vídeo não encontrada",
    "ffprobe_has_audio_stream": "faixa de áudio não encontrada",
    "duration_valid": "duração do MP4 inválida",
    "http_200_or_206": "vídeo indisponível para reprodução",
}


def video_fingerprint(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_manual_review(checks: Dict[str, bool], review: Any, path: str) -> Dict[str, Any]:
    """Only server-owned UnifiedVideo review records can waive quality alerts."""
    review = review if isinstance(review, dict) else {}
    records = review.get("asset_approvals") or {}
    if not isinstance(records, dict):
        return {}
    digest = video_fingerprint(path) if records else ""
    applied = {}
    for asset, record in records.items():
        if not isinstance(record, dict) or not record.get("reviewed_by"):
            continue
        if not digest or record.get("video_sha256") != digest:
            continue
        waived = ASSET_CHECKS.get(asset, set()) & OVERRIDABLE_CHECKS
        if asset == "all":
            waived = OVERRIDABLE_CHECKS
        for name in waived:
            if name in checks and not checks[name]:
                applied[name] = dict(record)
                checks[name] = True
    return applied
