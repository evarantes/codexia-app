"""Google Drive archive for production media.

The worker sends bytes directly through the central API. Drive resumable uploads
are chunked in memory, so the main server does not need a second full-size copy.
Local files are retained by callers until Drive confirms size and MD5 checksum.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterable, Dict, Optional
from urllib.parse import quote

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sqlalchemy import desc
from cryptography.fernet import Fernet, InvalidToken

from app.database import SessionLocal
from app.models import Settings
from app.routers.auth import SECRET_KEY

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
ROOT_FOLDER_NAME = "Codexia Archive"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # multiple of Google's 256 KiB requirement
_LOCK = threading.RLock()
_TOKEN_CACHE: Dict[str, tuple[str, float]] = {}
_DRIVE_TOKEN_PREFIX = "enc:v1:"


def _token_cipher() -> Fernet:
    key = hashlib.sha256(str(SECRET_KEY).encode("utf-8")).digest()
    import base64
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_refresh_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    return _DRIVE_TOKEN_PREFIX + _token_cipher().encrypt(token.encode("utf-8")).decode("ascii")


def _decrypt_refresh_token(token: str) -> str:
    token = str(token or "").strip()
    if not token.startswith(_DRIVE_TOKEN_PREFIX):
        return token  # accepts pre-encryption migrations and env overrides
    try:
        return _token_cipher().decrypt(token[len(_DRIVE_TOKEN_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise GoogleDriveArchiveError("A chave SECRET_KEY mudou; reconecte a conta do Google Drive.") from exc


class GoogleDriveArchiveError(RuntimeError):
    pass


class DriveAssetConflict(GoogleDriveArchiveError):
    def __init__(self, existing_sha256: str):
        self.existing_sha256 = existing_sha256
        super().__init__("O Google Drive já contém uma versão diferente deste ativo.")


def _quoted(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _valid_task(task: str) -> str:
    task = str(task or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task):
        raise ValueError("ID de produção inválido")
    return task


def _valid_kind_filename(kind: str, filename: str) -> tuple[str, str]:
    from app.services.central_asset_transfer import EXTENSIONS

    filename = str(filename or "")
    if kind not in EXTENSIONS or Path(filename).name != filename or not re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", filename):
        raise ValueError("Ativo inválido")
    if Path(filename).suffix.lower() not in EXTENSIONS[kind]:
        raise ValueError("Extensão inválida")
    return kind, filename


class GoogleDriveArchive:
    def __init__(self, user_id: Optional[int] = None):
        self.user_id = user_id

    def _settings(self) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            row = None
            if self.user_id is not None:
                row = db.query(Settings).filter(Settings.user_id == int(self.user_id)).order_by(desc(Settings.id)).first()
            if row is None:
                row = (db.query(Settings)
                       .filter(Settings.google_drive_refresh_token.isnot(None))
                       .order_by(desc(Settings.id)).first())
            return {
                "id": getattr(row, "id", None),
                "user_id": getattr(row, "user_id", None),
                "refresh_token": _decrypt_refresh_token(getattr(row, "google_drive_refresh_token", None) or ""),
                "folder_id": (getattr(row, "google_drive_folder_id", None) or "").strip(),
                "client_id": (os.getenv("GOOGLE_DRIVE_CLIENT_ID") or getattr(row, "youtube_client_id", None) or os.getenv("YOUTUBE_CLIENT_ID") or "").strip(),
                "client_secret": (os.getenv("GOOGLE_DRIVE_CLIENT_SECRET") or getattr(row, "youtube_client_secret", None) or os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip(),
            }
        finally:
            db.close()

    def is_configured(self) -> bool:
        values = self._settings()
        return bool((values["refresh_token"] or os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN")) and values["client_id"] and values["client_secret"])

    def credentials(self) -> Credentials:
        values = self._settings()
        refresh_token = values["refresh_token"] or (os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN") or "").strip()
        if not (refresh_token and values["client_id"] and values["client_secret"]):
            raise GoogleDriveArchiveError("Google Drive ainda não foi conectado nas Configurações.")
        cache_key = hashlib.sha256(f"{refresh_token}\0{values['client_id']}\0{values['client_secret']}".encode()).hexdigest()
        with _LOCK:
            cached = _TOKEN_CACHE.get(cache_key)
            if cached and cached[1] > time.time() + 60:
                return Credentials(
                    token=cached[0], refresh_token=refresh_token,
                    token_uri="https://oauth2.googleapis.com/token", client_id=values["client_id"],
                    client_secret=values["client_secret"], scopes=[DRIVE_SCOPE],
                    expiry=datetime.datetime.utcfromtimestamp(cached[1]),
                )
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=values["client_id"],
            client_secret=values["client_secret"],
            scopes=[DRIVE_SCOPE],
        )
        try:
            credentials.refresh(GoogleRequest())
        except Exception as exc:
            raise GoogleDriveArchiveError("Não foi possível renovar o acesso ao Google Drive. Reconecte a conta nas Configurações.") from exc
        if credentials.token and credentials.expiry:
            with _LOCK:
                _TOKEN_CACHE[cache_key] = (credentials.token, credentials.expiry.timestamp())
        return credentials

    def drive_service(self):
        return build("drive", "v3", credentials=self.credentials(), cache_discovery=False)

    def _save_folder_id(self, settings_id: Optional[int], folder_id: str) -> None:
        if not settings_id:
            return
        db = SessionLocal()
        try:
            row = db.query(Settings).filter(Settings.id == int(settings_id)).first()
            if row is not None:
                row.google_drive_folder_id = folder_id
                db.commit()
        finally:
            db.close()

    def _get_root_folder(self, service, values: Dict[str, Any]) -> str:
        folder_id = values["folder_id"] or (os.getenv("GOOGLE_DRIVE_FOLDER_ID") or "").strip()
        if folder_id:
            try:
                found = service.files().get(fileId=folder_id, fields="id,mimeType,trashed").execute()
                if found.get("mimeType") == FOLDER_MIME and not found.get("trashed"):
                    return folder_id
            except Exception:
                pass
        query = "name = " + _quoted(ROOT_FOLDER_NAME) + " and mimeType = " + _quoted(FOLDER_MIME) + " and trashed = false"
        found = service.files().list(q=query, pageSize=10, fields="files(id,name)").execute().get("files") or []
        if found:
            folder_id = str(found[0]["id"])
        else:
            created = service.files().create(body={
                "name": ROOT_FOLDER_NAME,
                "mimeType": FOLDER_MIME,
                "appProperties": {"codexia_archive": "root-v1"},
            }, fields="id").execute()
            folder_id = str(created["id"])
        self._save_folder_id(values.get("id"), folder_id)
        return folder_id

    def root_folder_id(self) -> str:
        values = self._settings()
        try:
            with _LOCK:
                return self._get_root_folder(self.drive_service(), values)
        except GoogleDriveArchiveError:
            raise
        except Exception as exc:
            raise GoogleDriveArchiveError("Falha ao acessar/criar a pasta Codexia Archive no Google Drive.") from exc

    def status(self) -> Dict[str, Any]:
        if not self.is_configured():
            return {"connected": False, "folder": ROOT_FOLDER_NAME}
        folder_id = self.root_folder_id()
        return {"connected": True, "folder": ROOT_FOLDER_NAME, "folder_id": folder_id}

    def archive_usage(self) -> Dict[str, Any]:
        """Return storage used by Codexia-owned archive files, without wider Drive scopes."""
        if not self.is_configured():
            return {
                "status": "not_connected",
                "connected": False,
                "archive_files": 0,
                "archive_bytes": 0,
            }

        service = self.drive_service()
        query = (
            "appProperties has { key='archive_version' and value='1' } "
            "and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
        )
        page_token = None
        file_count = 0
        total_bytes = 0
        while True:
            response = service.files().list(
                q=query,
                pageSize=1000,
                pageToken=page_token,
                fields="nextPageToken,files(size)",
            ).execute()
            for item in response.get("files") or []:
                file_count += 1
                try:
                    total_bytes += max(0, int(item.get("size") or 0))
                except (TypeError, ValueError):
                    continue
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return {
            "status": "connected",
            "connected": True,
            "archive_files": file_count,
            "archive_bytes": total_bytes,
        }

    def _task_folder(self, service, root_id: str, task: str) -> str:
        task = _valid_task(task)
        query = (
            f"{_quoted(root_id)} in parents and name = {_quoted(task)} "
            f"and mimeType = {_quoted(FOLDER_MIME)} and trashed = false"
        )
        found = service.files().list(q=query, pageSize=10, fields="files(id,name)").execute().get("files") or []
        if found:
            return str(found[0]["id"])
        created = service.files().create(body={
            "name": task,
            "mimeType": FOLDER_MIME,
            "parents": [root_id],
            "appProperties": {"codexia_task": task},
        }, fields="id").execute()
        return str(created["id"])

    def _find_file(self, service, parent_id: str, kind: str, filename: str) -> Optional[Dict[str, Any]]:
        query = (
            f"{_quoted(parent_id)} in parents and name = {_quoted(filename)} "
            f"and appProperties has {{ key='kind' and value={_quoted(kind)} }} and trashed = false"
        )
        files = service.files().list(
            q=query, pageSize=10,
            fields="files(id,name,size,md5Checksum,appProperties,mimeType)",
        ).execute().get("files") or []
        return files[0] if files else None

    def _prepare_upload(self, task: str, kind: str, filename: str, sha256: str, size: int,
                        expected_previous_sha256: str = ""):
        values = self._settings()
        service = self.drive_service()
        root_id = self._get_root_folder(service, values)
        parent_id = self._task_folder(service, root_id, task)
        existing = self._find_file(service, parent_id, kind, filename)
        existing_sha = str(((existing or {}).get("appProperties") or {}).get("sha256") or "")
        existing_size = int((existing or {}).get("size") or 0)
        if existing and existing_sha == sha256 and existing_size == size:
            return values, service, parent_id, existing, True
        if existing and existing_sha and existing_sha != expected_previous_sha256:
            raise DriveAssetConflict(existing_sha)
        return values, service, parent_id, existing, False

    @staticmethod
    def _app_properties(task: str, kind: str, sha256: str) -> Dict[str, str]:
        return {"task": task, "kind": kind, "sha256": sha256, "archive_version": "1"}

    def upload_path(self, task: str, kind: str, filename: str, path: str | Path,
                    expected_previous_sha256: str = "") -> Dict[str, Any]:
        task = _valid_task(task)
        kind, filename = _valid_kind_filename(kind, filename)
        path = Path(path)
        sha = hashlib.sha256()
        md5 = hashlib.md5()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                sha.update(chunk)
                md5.update(chunk)
        sha256 = sha.hexdigest()
        md5sum = md5.hexdigest()
        if not size:
            raise GoogleDriveArchiveError("Arquivo vazio não pode ser arquivado.")
        values, service, parent_id, existing, already = self._prepare_upload(
            task, kind, filename, sha256, size, expected_previous_sha256,
        )
        if already:
            return {"kind": kind, "filename": filename, "drive_file_id": existing["id"], "sha256": sha256, "size": size}
        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        media = MediaFileUpload(str(path), mimetype=mimetype, resumable=True, chunksize=UPLOAD_CHUNK_SIZE)
        body = {"appProperties": self._app_properties(task, kind, sha256)}
        if existing:
            request = service.files().update(fileId=existing["id"], body=body, media_body=media,
                                             fields="id,name,size,md5Checksum,appProperties")
        else:
            body.update({"name": filename, "mimeType": mimetype, "parents": [parent_id]})
            request = service.files().create(body=body, media_body=media,
                                             fields="id,name,size,md5Checksum,appProperties")
        try:
            response = None
            while response is None:
                _, response = request.next_chunk()
        except Exception as exc:
            raise GoogleDriveArchiveError("Upload retomável para o Google Drive falhou; cópia original preservada.") from exc
        if str(response.get("md5Checksum") or "").lower() != md5sum or int(response.get("size") or -1) != size:
            raise GoogleDriveArchiveError("Google Drive não confirmou checksum/tamanho; cópia original preservada.")
        return {"kind": kind, "filename": filename, "drive_file_id": response["id"], "sha256": sha256, "size": size}

    async def upload_stream(self, task: str, kind: str, filename: str, chunks: AsyncIterable[bytes],
                            expected_sha256: str, expected_size: int,
                            expected_previous_sha256: str = "") -> Dict[str, Any]:
        task = _valid_task(task)
        kind, filename = _valid_kind_filename(kind, filename)
        expected_size = int(expected_size)
        if expected_size <= 0:
            raise GoogleDriveArchiveError("Tamanho do ativo inválido.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256 or "")):
            raise GoogleDriveArchiveError("SHA256 inválido.")
        try:
            values, service, parent_id, existing, already = self._prepare_upload(
                task, kind, filename, expected_sha256, expected_size, expected_previous_sha256,
            )
        except (DriveAssetConflict, GoogleDriveArchiveError):
            raise
        except Exception as exc:
            raise GoogleDriveArchiveError("Não foi possível preparar o ativo no Google Drive; fonte preservada.") from exc
        if already:
            return {"kind": kind, "filename": filename, "drive_file_id": existing["id"], "sha256": expected_sha256, "size": expected_size}

        credentials = self.credentials()
        # Make a fresh access token available before starting the resumable HTTP session.
        if not credentials.valid:
            credentials.refresh(GoogleRequest())
        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        metadata = {
            "appProperties": self._app_properties(task, kind, expected_sha256),
        }
        query = {"uploadType": "resumable", "fields": "id,name,size,md5Checksum,appProperties"}
        if existing:
            start_url = f"{DRIVE_UPLOAD_URL}/{quote(str(existing['id']), safe='')}"
            method = "PATCH"
        else:
            start_url = DRIVE_UPLOAD_URL
            method = "POST"
            metadata.update({"name": filename, "mimeType": mimetype, "parents": [parent_id]})
        start_headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mimetype,
            "X-Upload-Content-Length": str(expected_size),
        }
        try:
            start = requests.request(method, start_url, params=query, json=metadata, headers=start_headers,
                                     timeout=(15, 60), allow_redirects=False)
        except Exception as exc:
            raise GoogleDriveArchiveError("Falha de rede ao iniciar upload no Google Drive; fonte preservada.") from exc
        if start.status_code not in (200, 201):
            raise GoogleDriveArchiveError(f"Google Drive recusou o início do upload (HTTP {start.status_code}).")
        session_url = start.headers.get("Location")
        if not session_url:
            raise GoogleDriveArchiveError("Google Drive não retornou a URL da sessão de upload.")

        sha = hashlib.sha256()
        md5 = hashlib.md5()
        buffer = bytearray()
        received = 0
        uploaded = 0
        final_metadata = None

        async def send_chunk(data: bytes, final: bool):
            nonlocal uploaded, final_metadata
            end = uploaded + len(data) - 1
            headers = {
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {uploaded}-{end}/{expected_size}",
            }
            try:
                response = await asyncio.to_thread(
                    requests.put, session_url, data=data, headers=headers, timeout=(15, 180), allow_redirects=False,
                )
            except Exception as exc:
                raise GoogleDriveArchiveError("Falha de rede durante upload retomável; fonte preservada.") from exc
            if final:
                if response.status_code not in (200, 201):
                    raise GoogleDriveArchiveError(f"Google Drive não concluiu o upload (HTTP {response.status_code}).")
                try:
                    final_metadata = response.json()
                except Exception as exc:
                    raise GoogleDriveArchiveError("Google Drive retornou confirmação inválida do upload.") from exc
            elif response.status_code != 308:
                raise GoogleDriveArchiveError(f"Google Drive interrompeu o upload retomável (HTTP {response.status_code}).")
            uploaded = end + 1

        async for incoming in chunks:
            if not incoming:
                continue
            received += len(incoming)
            if received > expected_size:
                raise GoogleDriveArchiveError("O corpo recebido excedeu o tamanho informado; fonte preservada.")
            sha.update(incoming)
            md5.update(incoming)
            buffer.extend(incoming)
            while uploaded < expected_size:
                wanted = min(UPLOAD_CHUNK_SIZE, expected_size - uploaded)
                if len(buffer) < wanted:
                    break
                part = bytes(buffer[:wanted])
                del buffer[:wanted]
                await send_chunk(part, uploaded + len(part) == expected_size)

        if received != expected_size or buffer:
            raise GoogleDriveArchiveError("O corpo recebido não corresponde ao tamanho informado; fonte preservada.")
        if sha.hexdigest() != expected_sha256:
            raise GoogleDriveArchiveError("SHA256 do ativo não confere; cópia temporária preservada.")
        md5sum = md5.hexdigest()
        if not final_metadata or str(final_metadata.get("md5Checksum") or "").lower() != md5sum or int(final_metadata.get("size") or -1) != expected_size:
            raise GoogleDriveArchiveError("Google Drive não confirmou checksum/tamanho; cópia temporária preservada.")
        return {"kind": kind, "filename": filename, "drive_file_id": final_metadata["id"],
                "sha256": expected_sha256, "size": expected_size}

    def get_metadata(self, file_id: str) -> Dict[str, Any]:
        try:
            return self.drive_service().files().get(fileId=file_id, fields="id,name,size,mimeType,md5Checksum").execute()
        except Exception as exc:
            raise GoogleDriveArchiveError("Não foi possível consultar o ativo no Google Drive.") from exc

    def stream_response(self, entry: Dict[str, Any], range_header: str = "", media_type: str = "application/octet-stream",
                        head: bool = False):
        from fastapi import HTTPException
        from fastapi.responses import Response, StreamingResponse

        file_id = str(entry.get("drive_file_id") or "").strip()
        if not file_id:
            raise HTTPException(status_code=404, detail="Ativo não arquivado no Google Drive.")
        try:
            size = int(entry.get("size") or 0)
            if head:
                meta = self.get_metadata(file_id)
                size = int(meta.get("size") or size)
                return StreamingResponse(iter(()), headers={
                    "Content-Length": str(size), "Accept-Ranges": "bytes", "Cache-Control": "no-store",
                }, media_type=media_type)
            headers = {"Authorization": f"Bearer {self.credentials().token}"}
            if range_header:
                headers["Range"] = range_header
            response = requests.get(
                f"{DRIVE_FILES_URL}/{quote(file_id, safe='')}", params={"alt": "media"}, headers=headers,
                stream=True, timeout=(15, 300), allow_redirects=True,
            )
            if response.status_code == 416:
                range_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
                if response.headers.get("Content-Range"):
                    range_headers["Content-Range"] = response.headers["Content-Range"]
                response.close()
                return Response(status_code=416, headers=range_headers)
            if response.status_code not in (200, 206):
                response.close()
                raise HTTPException(status_code=502, detail="Falha ao ler o arquivo do Google Drive.")
            forwarded = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
            for key in ("Content-Length", "Content-Range"):
                if response.headers.get(key):
                    forwarded[key] = response.headers[key]

            def body():
                try:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            yield chunk
                finally:
                    response.close()

            return StreamingResponse(body(), status_code=response.status_code, media_type=media_type, headers=forwarded)
        except HTTPException:
            raise
        except GoogleDriveArchiveError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Falha ao transmitir o arquivo do Google Drive.") from exc
