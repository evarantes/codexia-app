"""Translate approved assistant briefs to existing Codexia music API payloads.

No provider calls are made here. Existing routes:
    POST /music/generate
    GET  /music/task/{task_id}
    POST /music/clip/task
    GET  /music/tasks

Use Codexia's authenticated user session for the production calls. Do not
expose a privileged internal user token to the assistant.
"""
from __future__ import annotations

from typing import Any


def music_payload(brief: dict[str, Any]) -> dict[str, Any]:
    voice = (brief.get("voice") or "").lower()
    return {
        "title": brief["title"],
        "lyrics": brief["lyrics"],
        "genre": brief["style"],
        "vocal_gender": {"male": "m", "female": "f"}.get(voice),
    }


def clip_payload(brief: dict[str, Any], music_filename: str) -> dict[str, Any]:
    if not music_filename or "/" in music_filename or "\\" in music_filename:
        raise ValueError("A valid saved music filename is required")
    return {
        "title": brief["title"],
        "lyrics": brief["lyrics"],
        "music_filename": music_filename,
        "author_text": brief.get("author_text") or "",
        "watermark_enabled": True,
        "captions_enabled": True,
        "aspect_ratio": brief.get("aspect_ratio", "16:9"),
        "auto_upload_youtube": False,
    }


def eligible_assets(brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Only rights-confirmed references; importing into clip scenes is future work."""
    return [a for a in brief.get("assets", []) if a.get("rights_confirmed") is True]
