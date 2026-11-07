"""Google Drive backed private storage for Entre Nós Dois pilot assets.

Only the central index and paid-request reservations remain on the application
volume. Media bytes are uploaded to the user's isolated Drive task folder.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

class EntreNosDoisDriveError(RuntimeError):
    pass


def task_for_user(user_id: int) -> str:
    if type(user_id) is not int or user_id <= 0:
        raise EntreNosDoisDriveError("Usuário inválido.")
    return f"entre-nos-dois-user-{user_id}"


def store_asset_bytes(*, user_id: int, kind: str, filename: str, body: bytes, metadata: dict | None = None) -> dict:
    """Upload and checksum-verify an asset before registering its Drive pointer."""
    if not isinstance(body, bytes) or not body:
        raise EntreNosDoisDriveError("Arquivo vazio ou inválido.")
    task = task_for_user(user_id)
    try:
        from app.services.google_drive_archive import GoogleDriveArchive
        from app.services.central_asset_transfer import central_index, register
        with tempfile.TemporaryDirectory(prefix="codexia-entre-nos-dois-") as tmp:
            path = Path(tmp) / filename
            path.write_bytes(body)
            archive = GoogleDriveArchive.for_task(task, user_id=user_id)
            if not archive.is_configured():
                raise EntreNosDoisDriveError("Conecte o Google Drive em Configurações antes de enviar arquivos.")
            previous = next(
                (item.get("sha256", "") for item in central_index(task, verify=False)
                 if item.get("kind") == kind and item.get("filename") == filename and item.get("drive_file_id")),
                "",
            )
            entry = archive.upload_path(
                task, kind, filename, path, expected_previous_sha256=previous,
            )
            if entry.get("sha256") != hashlib.sha256(body).hexdigest() or int(entry.get("size") or -1) != len(body):
                raise EntreNosDoisDriveError("O Google Drive não confirmou a integridade do arquivo.")
            if metadata:
                entry.update(metadata)
            register(task, entry)
            return dict(entry)
    except EntreNosDoisDriveError:
        raise
    except Exception as exc:
        raise EntreNosDoisDriveError("Falha ao armazenar o arquivo no Google Drive; o upload não foi confirmado.") from exc


def find_asset(*, user_id: int, kind: str, filename: str) -> dict | None:
    task = task_for_user(user_id)
    if not drive_storage_health(user_id=user_id).get("ready"):
        return None
    try:
        from app.services.central_asset_transfer import central_index
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
        from app.services.google_drive_archive import GoogleDriveArchive
        archive = GoogleDriveArchive.for_asset(entry)
        return archive.stream_response(entry, range_header=range_header, media_type=media_type, head=head)
    except Exception as exc:
        raise EntreNosDoisDriveError("Não foi possível ler o arquivo privado no Google Drive.") from exc


def drive_storage_health(*, user_id: int | None = None) -> dict:
    try:
        from app.services.google_drive_archive import GoogleDriveArchive
        task = task_for_user(user_id) if user_id is not None else "entre-nos-dois-health"
        archive = GoogleDriveArchive.for_task(task, user_id=user_id)
        ready = archive.is_configured()
    except Exception:
        ready = False
    return {
        "ready": ready,
        "reason": "drive_configured" if ready else "google_drive_not_configured",
    }


def _video_row(*, user_id: int, video_id: int):
    from app.database import SessionLocal
    from app.models import UnifiedVideo
    db = SessionLocal()
    try:
        row = db.query(UnifiedVideo).filter(
            UnifiedVideo.id == int(video_id),
            UnifiedVideo.user_id == int(user_id),
            UnifiedVideo.source_module == "entre_nos_dois",
        ).first()
        if row is None:
            raise EntreNosDoisDriveError("Episódio não encontrado neste projeto.")
        return row
    finally:
        db.close()


def find_video_asset(*, user_id: int, video_id: int) -> dict | None:
    row = _video_row(user_id=user_id, video_id=video_id)
    task = str(row.task_id or "").strip()
    if not task:
        return None
    try:
        from app.services.central_asset_transfer import central_index
        return next(
            (item for item in central_index(task, verify=False)
             if item.get("kind") == "video"
             and item.get("filename") == f"episodio-{int(video_id)}.mp4"
             and item.get("drive_file_id")),
            None,
        )
    except Exception as exc:
        raise EntreNosDoisDriveError("Não foi possível consultar o arquivo do episódio no Drive.") from exc


def archive_video_file(*, user_id: int, video_id: int, path: str | Path) -> dict:
    """Archive one rendered MP4; delete its local copy only after Drive confirms it."""
    import json
    import os
    from app.database import SessionLocal
    from app.config import UNIFIED_VIDEO_DIR
    from app.models import UnifiedVideo
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise EntreNosDoisDriveError("O MP4 final não está disponível para arquivamento.")
    row = _video_row(user_id=user_id, video_id=video_id)
    task = str(row.task_id or "").strip()
    if not task:
        raise EntreNosDoisDriveError("A tarefa de produção não foi vinculada ao episódio.")
    try:
        from app.services.google_drive_archive import GoogleDriveArchive
        from app.services.central_asset_transfer import central_index, register
        archive = GoogleDriveArchive.for_task(task, user_id=user_id)
        if not archive.is_configured():
            raise EntreNosDoisDriveError("Conecte o Google Drive antes de arquivar o episódio.")
        filename = f"episodio-{int(video_id)}.mp4"
        previous = next(
            (item.get("sha256", "") for item in central_index(task, verify=False)
             if item.get("kind") == "video" and item.get("filename") == filename
             and item.get("drive_file_id")),
            "",
        )
        entry = archive.upload_path(
            task, "video", filename, source, expected_previous_sha256=previous,
        )
        if int(entry.get("size") or -1) != source.stat().st_size:
            raise EntreNosDoisDriveError("O Drive não confirmou o tamanho do MP4.")
        entry.update({"video_id": int(video_id), "user_id": int(user_id)})
        register(task, entry)

        # Point the user interface to Drive. Keep task result as an audit trail
        # without exposing filesystem paths to the browser.
        db = SessionLocal()
        try:
            current = db.query(UnifiedVideo).filter(
                UnifiedVideo.id == int(video_id),
                UnifiedVideo.user_id == int(user_id),
                UnifiedVideo.source_module == "entre_nos_dois",
            ).first()
            if current is None:
                raise EntreNosDoisDriveError("Episódio não encontrado durante a atualização.")
            payload = {}
            try:
                payload = json.loads(current.result_json or "{}")
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["entre_nos_dois_drive_video"] = {
                "drive_file_id": entry.get("drive_file_id"),
                "drive_account_id": entry.get("drive_account_id"),
                "filename": filename,
                "size": entry.get("size"),
                "sha256": entry.get("sha256"),
            }
            current.result_json = json.dumps(payload, ensure_ascii=False)
            current.video_path = None
            current.video_url = f"/studio/entre-nos-dois/videos/{int(video_id)}/file"
            current.last_message = "Vídeo arquivado e conferido no Google Drive."
            current.current_step = "drive_archived"
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        # Retain cache only when it is outside the application's managed render
        # directory; never unlink an arbitrary path supplied by a request.
        root = Path(UNIFIED_VIDEO_DIR).resolve()
        resolved = source.resolve()
        try:
            inside = os.path.commonpath([str(root), str(resolved)]) == str(root)
        except (OSError, ValueError):
            inside = False
        if inside and resolved.is_file():
            resolved.unlink()
        return dict(entry)
    except EntreNosDoisDriveError:
        raise
    except Exception as exc:
        raise EntreNosDoisDriveError(
            "Falha ao confirmar o arquivamento no Google Drive; a cópia local foi preservada."
        ) from exc


def archive_finished_video_task(task_id: str) -> dict | None:
    """Archive a finished Entre Nós Dois render; unrelated Codexia jobs are ignored."""
    from app.database import SessionLocal
    from app.models import UnifiedVideo
    db = SessionLocal()
    try:
        row = db.query(UnifiedVideo).filter(
            UnifiedVideo.task_id == str(task_id),
            UnifiedVideo.source_module == "entre_nos_dois",
        ).order_by(UnifiedVideo.id.desc()).first()
        if row is None or not row.user_id or not row.video_path:
            return None
        user_id, video_id, path = int(row.user_id), int(row.id), str(row.video_path)
    finally:
        db.close()
    return archive_video_file(user_id=user_id, video_id=video_id, path=path)
