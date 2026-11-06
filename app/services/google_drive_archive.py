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
from app.models import GoogleDriveAccount, Settings, UnifiedVideo
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


def encrypt_drive_secret(value: str) -> str:
    """Encrypt a Google Drive secret before persisting it."""
    return encrypt_refresh_token(value)


def decrypt_drive_secret(value: str) -> str:
    return _decrypt_refresh_token(value)


class GoogleDriveArchiveError(RuntimeError):
    pass


class DriveAssetConflict(GoogleDriveArchiveError):
    def __init__(self, existing_sha256: str):
        self.existing_sha256 = existing_sha256
        super().__init__("O Google Drive já contém uma versão diferente deste ativo.")


def google_drive_oauth_client_credentials(settings: Optional[Settings]) -> tuple[str, str]:
    """Resolve Google Drive OAuth credentials without reading YouTube settings."""
    drive_client_id = str(getattr(settings, "google_drive_client_id", None) or "").strip()
    drive_secret = str(getattr(settings, "google_drive_client_secret", None) or "").strip()
    if drive_client_id and drive_secret:
        return drive_client_id, decrypt_drive_secret(drive_secret).strip()

    env_drive_id = str(os.getenv("GOOGLE_DRIVE_CLIENT_ID") or "").strip()
    env_drive_secret = str(os.getenv("GOOGLE_DRIVE_CLIENT_SECRET") or "").strip()
    if env_drive_id and env_drive_secret:
        return env_drive_id, env_drive_secret
    return "", ""


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
    def __init__(self, user_id: Optional[int] = None, account_id: Optional[int] = None,
                 access_token: Optional[str] = None, folder_id: Optional[str] = None):
        self.user_id = user_id
        self.account_id = int(account_id) if account_id not in (None, "") else None
        self.access_token = str(access_token or "").strip()
        self.access_folder_id = str(folder_id or "").strip()

    @staticmethod
    def _ensure_legacy_account(db):
        legacy = db.query(GoogleDriveAccount).filter(
            GoogleDriveAccount.is_legacy.is_(True)
        ).order_by(GoogleDriveAccount.id.asc()).first()
        if legacy:
            return legacy
        if db.query(GoogleDriveAccount).count():
            return None
        settings = (db.query(Settings)
                    .filter(Settings.google_drive_refresh_token.isnot(None))
                    .order_by(desc(Settings.id)).first())
        token = str(getattr(settings, "google_drive_refresh_token", None) or "").strip()
        if not token:
            return None
        legacy = GoogleDriveAccount(
            label="Conta Google Drive existente",
            refresh_token=token,
            folder_id=getattr(settings, "google_drive_folder_id", None),
            enabled=True,
            is_primary=True,
            is_legacy=True,
        )
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        return legacy

    @classmethod
    def for_task(cls, task: str, user_id: Optional[int] = None) -> "GoogleDriveArchive":
        """Keep every project's files on one Drive account for its lifetime."""
        from app.services.central_asset_transfer import index_path, task_drive_account, task_key
        from app.services.production_manifest import _read_json

        task = task_key(task)
        pinned = task_drive_account(task)
        if pinned is not None:
            return cls(user_id=user_id, account_id=pinned)

        index = _read_json(index_path(task))
        files = index.get("files") or []
        legacy_refs = any(
            item.get("drive_file_id") and item.get("drive_account_id") in (None, "")
            for item in files if isinstance(item, dict)
        )
        db = SessionLocal()
        try:
            if legacy_refs:
                legacy = cls._ensure_legacy_account(db)
                if legacy is None:
                    legacy = db.query(GoogleDriveAccount).filter(
                        GoogleDriveAccount.is_primary.is_(True)
                    ).order_by(GoogleDriveAccount.id.asc()).first()
                if legacy:
                    return cls(user_id=user_id, account_id=legacy.id)

            accounts = db.query(GoogleDriveAccount).filter(
                GoogleDriveAccount.enabled.is_(True)
            ).order_by(GoogleDriveAccount.id.asc()).all()
            if accounts:
                # Rendezvous hashing moves only a subset of projects when an
                # account is added or paused, instead of reassigning all work.
                selected = max(
                    accounts,
                    key=lambda account: hashlib.sha256(
                        f"{task}:{account.id}".encode("utf-8")
                    ).digest(),
                )
                return cls(user_id=user_id, account_id=selected.id)
            if db.query(GoogleDriveAccount).count():
                raise GoogleDriveArchiveError(
                    "Nenhuma conta do Google Drive está ativa para novos projetos. Ative uma conta nas Configurações."
                )
            legacy = cls._ensure_legacy_account(db)
            if legacy:
                return cls(user_id=user_id, account_id=legacy.id)
        finally:
            db.close()
        return cls(user_id=user_id)

    @classmethod
    def for_asset(cls, entry: Dict[str, Any]) -> "GoogleDriveArchive":
        """Resolve the account that owns an asset, including pre-pool archives."""
        account_id = entry.get("drive_account_id")
        if account_id not in (None, ""):
            return cls(account_id=int(account_id))
        db = SessionLocal()
        try:
            account = cls._ensure_legacy_account(db)
            if account is None:
                account = db.query(GoogleDriveAccount).filter(
                    GoogleDriveAccount.is_primary.is_(True)
                ).order_by(GoogleDriveAccount.id.asc()).first()
            if account:
                return cls(account_id=account.id)
        finally:
            db.close()
        return cls()

    def _settings(self) -> Dict[str, Any]:
        if self.access_token:
            return {
                "id": None,
                "account_id": self.account_id,
                "user_id": self.user_id,
                "refresh_token": "",
                "folder_id": self.access_folder_id,
                "client_id": "",
                "client_secret": "",
            }
        db = SessionLocal()
        try:
            account = None
            if self.account_id is not None:
                account = db.query(GoogleDriveAccount).filter(
                    GoogleDriveAccount.id == self.account_id
                ).first()
                if account is None:
                    raise GoogleDriveArchiveError(
                        "A conta do Google Drive vinculada a este projeto não existe mais."
                    )
            else:
                account = db.query(GoogleDriveAccount).order_by(
                    GoogleDriveAccount.is_primary.desc(), GoogleDriveAccount.id.asc()
                ).first()
            row = None
            if self.user_id is not None:
                row = db.query(Settings).filter(Settings.user_id == int(self.user_id)).order_by(desc(Settings.id)).first()
            if row is None:
                row = (db.query(Settings)
                       .filter(Settings.google_drive_refresh_token.isnot(None))
                       .order_by(desc(Settings.id)).first())
            client_id, client_secret = google_drive_oauth_client_credentials(row)
            return {
                "id": getattr(row, "id", None),
                "account_id": getattr(account, "id", None),
                "user_id": getattr(row, "user_id", None),
                "refresh_token": _decrypt_refresh_token(
                    (getattr(account, "refresh_token", None) or getattr(row, "google_drive_refresh_token", None) or "")
                ),
                "folder_id": (getattr(account, "folder_id", None) or getattr(row, "google_drive_folder_id", None) or "").strip(),
                "client_id": client_id,
                "client_secret": client_secret,
            }
        finally:
            db.close()

    def is_configured(self) -> bool:
        if self.access_token:
            return True
        values = self._settings()
        return bool((values["refresh_token"] or os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN")) and values["client_id"] and values["client_secret"])

    def credentials(self) -> Credentials:
        if self.access_token:
            return Credentials(token=self.access_token, scopes=[DRIVE_SCOPE])
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

    def _save_folder_id(self, settings_id: Optional[int], folder_id: str,
                        account_id: Optional[int] = None) -> None:
        if not settings_id and not account_id:
            return
        db = SessionLocal()
        try:
            if account_id:
                row = db.query(GoogleDriveAccount).filter(GoogleDriveAccount.id == int(account_id)).first()
            else:
                row = db.query(Settings).filter(Settings.id == int(settings_id)).first()
            if row is not None:
                if account_id:
                    row.folder_id = folder_id
                else:
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
        self._save_folder_id(values.get("id"), folder_id, values.get("account_id"))
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

    @classmethod
    def archive_usage_all(cls, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Aggregate archive usage across active and historical Drive accounts."""
        db = SessionLocal()
        try:
            accounts = db.query(GoogleDriveAccount).order_by(
                GoogleDriveAccount.is_primary.desc(), GoogleDriveAccount.id.asc()
            ).all()
            metadata = [
                {"id": item.id, "label": item.label, "enabled": bool(item.enabled),
                 "is_primary": bool(item.is_primary)}
                for item in accounts
            ]
        finally:
            db.close()
        if not metadata:
            return cls(user_id=user_id).archive_usage()

        results = []
        total_files = 0
        total_bytes = 0
        errors = 0
        for account in metadata:
            try:
                usage = cls(account_id=account["id"]).archive_usage()
            except Exception:
                usage = {"status": "error", "connected": False, "archive_files": 0,
                         "archive_bytes": 0, "error": "Não foi possível consultar esta conta do Drive."}
            account.update(usage)
            total_files += int(usage.get("archive_files") or 0)
            total_bytes += int(usage.get("archive_bytes") or 0)
            errors += int(usage.get("status") == "error")
            results.append(account)
        return {
            "status": "partial" if errors else "connected",
            "connected": any(item.get("connected") for item in results),
            "archive_files": total_files,
            "archive_bytes": total_bytes,
            "accounts": results,
            "errors": errors,
        }

    @staticmethod
    def _find_or_create_folder(service, parent_id: str, name: str, properties: Dict[str, str]) -> str:
        query = (
            f"{_quoted(parent_id)} in parents and name = {_quoted(name)} "
            f"and mimeType = {_quoted(FOLDER_MIME)} and trashed = false"
        )
        found = service.files().list(q=query, pageSize=10, fields="files(id,name)").execute().get("files") or []
        if found:
            return str(found[0]["id"])
        created = service.files().create(body={
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
            "appProperties": properties,
        }, fields="id").execute()
        return str(created["id"])

    def _entre_nos_dois_parent(self, service, root_id: str, user_id: int) -> str:
        module_id = self._find_or_create_folder(
            service, root_id, "Entre Nós Dois", {"codexia_module": "entre_nos_dois"},
        )
        return self._find_or_create_folder(
            service, module_id, f"usuario-{int(user_id)}",
            {"codexia_module": "entre_nos_dois", "codexia_user_id": str(int(user_id))},
        )

    def _task_folder(self, service, root_id: str, task: str) -> str:
        task = _valid_task(task)
        # Studio portraits/audio use one stable user-scoped task key.
        direct = re.fullmatch(r"entre-nos-dois-user-(\d+)", task)
        if direct:
            user_folder = self._entre_nos_dois_parent(service, root_id, int(direct.group(1)))
            return self._find_or_create_folder(
                service, user_folder, "materiais",
                {"codexia_module": "entre_nos_dois", "codexia_kind": "materials"},
            )

        # Canonical video tasks keep their own index/task id, while their Drive
        # bytes are grouped under the module and user that own the episode.
        db = SessionLocal()
        try:
            video = (
                db.query(UnifiedVideo)
                .filter(
                    UnifiedVideo.task_id == task,
                    UnifiedVideo.source_module == "entre_nos_dois",
                )
                .order_by(UnifiedVideo.id.desc())
                .first()
            )
        except Exception:
            video = None
        finally:
            db.close()
        if video and video.user_id:
            user_folder = self._entre_nos_dois_parent(service, root_id, video.user_id)
            return self._find_or_create_folder(
                service, user_folder, f"episodio-{int(video.id)}",
                {"codexia_module": "entre_nos_dois", "codexia_video_id": str(int(video.id))},
            )

        return self._find_or_create_folder(
            service, root_id, task, {"codexia_task": task},
        )

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
        if (existing
                and (not expected_previous_sha256 or existing_sha != expected_previous_sha256)):
            # A pre-indexed Drive file without a SHA is not safe to replace.
            # Migration code can keep it and upload a separately named version.
            raise DriveAssetConflict(existing_sha or "unverified")
        return values, service, parent_id, existing, False

    def create_upload_session(self, task: str, kind: str, filename: str,
                              sha256: str, size: int,
                              expected_previous_sha256: str = "") -> Dict[str, Any]:
        """Authorize a resumable upload; media bytes go from the worker to Drive."""
        task = _valid_task(task)
        kind, filename = _valid_kind_filename(kind, filename)
        size = int(size)
        if size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", str(sha256 or "")):
            raise GoogleDriveArchiveError("Tamanho ou SHA256 inválido para upload.")
        if expected_previous_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", str(expected_previous_sha256 or "")
        ):
            raise GoogleDriveArchiveError("SHA256 anterior inválido para substituição.")
        values, _service, parent_id, existing, already = self._prepare_upload(
            task, kind, filename, sha256, size, expected_previous_sha256,
        )
        account_id = values.get("account_id")
        if already:
            return {
                "already_present": True,
                "entry": {
                    "kind": kind, "filename": filename,
                    "drive_file_id": existing["id"], "drive_account_id": account_id,
                    "sha256": sha256, "size": size,
                },
            }

        credentials = self.credentials()
        if not credentials.valid:
            credentials.refresh(GoogleRequest())
        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        metadata = {"appProperties": self._app_properties(task, kind, sha256)}
        params = {"uploadType": "resumable", "fields": "id,name,size,md5Checksum,appProperties"}
        if existing:
            url = f"{DRIVE_UPLOAD_URL}/{quote(str(existing['id']), safe='')}"
            method = "PATCH"
        else:
            url = DRIVE_UPLOAD_URL
            method = "POST"
            metadata.update({"name": filename, "mimeType": mimetype, "parents": [parent_id]})
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mimetype,
            "X-Upload-Content-Length": str(size),
        }
        try:
            response = requests.request(
                method, url, params=params, json=metadata, headers=headers,
                timeout=(15, 60), allow_redirects=False,
            )
        except Exception as exc:
            raise GoogleDriveArchiveError("Falha ao abrir sessão direta de upload no Google Drive.") from exc
        if response.status_code not in (200, 201):
            raise GoogleDriveArchiveError(
                f"Google Drive recusou a sessão direta de upload (HTTP {response.status_code})."
            )
        session_url = str(response.headers.get("Location") or "").strip()
        if not session_url.startswith("https://"):
            raise GoogleDriveArchiveError("Google Drive não retornou uma sessão HTTPS válida.")
        return {
            "already_present": False,
            "upload_url": session_url,
            "drive_account_id": account_id,
            "kind": kind,
            "filename": filename,
            "sha256": sha256,
            "size": size,
        }

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
            return {"kind": kind, "filename": filename, "drive_file_id": existing["id"],
                    "drive_account_id": values.get("account_id"), "sha256": sha256, "size": size}
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
        return {"kind": kind, "filename": filename, "drive_file_id": response["id"],
                "drive_account_id": values.get("account_id"), "sha256": sha256, "size": size}

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
            return {"kind": kind, "filename": filename, "drive_file_id": existing["id"],
                    "drive_account_id": values.get("account_id"), "sha256": expected_sha256, "size": expected_size}

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
                "drive_account_id": values.get("account_id"), "sha256": expected_sha256, "size": expected_size}

    def get_metadata(self, file_id: str) -> Dict[str, Any]:
        try:
            return self.drive_service().files().get(
                fileId=file_id, fields="id,name,size,mimeType,md5Checksum,appProperties"
            ).execute()
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
