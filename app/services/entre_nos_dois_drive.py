"""Google Drive backed private storage for Entre Nós Dois pilot assets.

Only the central index and paid-request reservations remain on the application
volume. Media bytes are uploaded to the user's isolated Drive task folder.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from app.services.google_drive_archive import GoogleDriveArchive
from app.services.central_asset_transfer import central_index, register


class EntreNosDoisDriveError(RuntimeError):
    pass


def task_for_user(user_id: int) -> str:
    if type(user_id) is not int or user_id <= 0:
        raise EntreNosDoisDriveError("Usuário inválido.")
    return f"entre-nos-dois-user-{user_id}"


def store_asset_bytes(*, user_id: int, kind: str, filename: str, body: bytes) -> dict:
    """Upload and checksum-verify an asset before registering its Drive pointer."""
    if not isinstance(body, bytes) or not body:
        raise EntreNosDoisDriveError("Arquivo vazio ou inválido.")
    task = task_for_user(user_id)
    try:
        with tempfile.TemporaryDirectory(prefix="codexia-entre-nos-dois-") as tmp:
            path = Path(tmp) / filename
            path.write_bytes(body)
            archive = GoogleDriveArchive.for_task(task, user_id=user_id)
            if not archive.is_configured():
                raise EntreNosDoisDriveError("Conecte o Google Drive em Configurações antes de enviar arquivos.")
            entry = archive.upload_path(task, kind, filename, path)
            if entry.get("sha256") != hashlib.sha256(body).hexdigest() or int(entry.get("size") or -1) != len(body):
                raise EntreNosDoisDriveError("O Google Drive não confirmou a integridade do arquivo.")
            register(task, entry)
            return dict(entry)
    except EntreNosDoisDriveError:
        raise
    except Exception as exc:
        raise EntreNosDoisDriveError("Falha ao armazenar o arquivo no Google Drive; o upload não foi confirmado.") from exc


def find_asset(*, user_id: int, kind: str, filename: str) -> dict | None:
    task = task_for_user(user_id)
    try:
        return next(
            (item for item in central_index(task, verify=False)
             if item.get("kind") == kind and item.get("filename") == filename and item.get("drive_file_id")),
            None,
        )
    except Exception as exc:
        raise EntreNosDoisDriveError("Não foi possível consultar o índice privado do Google Drive.") from exc


def asset_present(*, user_id: int, kind: str, filename: str) -> bool:
    return find_asset(user_id=user_id, kind=kind, filename=filename) is not None


def stream_asset(entry: dict, *, media_type: str, range_header: str = "", head: bool = False):
    try:
        archive = GoogleDriveArchive.for_asset(entry)
        return archive.stream_response(entry, range_header=range_header, media_type=media_type, head=head)
    except Exception as exc:
        raise EntreNosDoisDriveError("Não foi possível ler o arquivo privado no Google Drive.") from exc


def drive_storage_health(*, user_id: int | None = None) -> dict:
    try:
        task = task_for_user(user_id) if user_id is not None else "entre-nos-dois-health"
        archive = GoogleDriveArchive.for_task(task, user_id=user_id)
        ready = archive.is_configured()
    except Exception:
        ready = False
    return {
        "ready": ready,
        "reason": "drive_configured" if ready else "google_drive_not_configured",
    }
