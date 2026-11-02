"""OAuth connection and controlled migration to the Google Drive archive."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.database import SessionLocal, get_db
from app.models import GoogleDriveAccount, Settings, User
from app.routers.auth import SECRET_KEY, get_current_admin_user
from app.routers.settings import _get_or_create_settings_row
from app.services.central_asset_transfer import (
    digest, index_path, pin_task_drive_account, register as register_asset,
    roots, task_index_lock, task_key,
)
from app.services.google_drive_archive import (
    DRIVE_SCOPE,
    DriveAssetConflict,
    GoogleDriveArchive,
    GoogleDriveArchiveError,
    ROOT_FOLDER_NAME,
    decrypt_drive_secret,
    encrypt_drive_secret,
    encrypt_refresh_token,
)

router = APIRouter(prefix="/settings/google-drive", tags=["Google Drive archive"])
_STATE = URLSafeTimedSerializer(SECRET_KEY, salt="codexia-google-drive-oauth-v1")
_ARCHIVABLE_STATUSES = {
    "completed", "awaiting_review", "approved", "rendered_upload_failed",
    "failed", "paused", "cancelled", "published",
}
_MIGRATION_EXTENSIONS = {
    "video": {".mp4", ".mov", ".mkv", ".webm"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    "image": {".png", ".jpg", ".jpeg", ".webp"},
}
_UPLOAD_SESSION_TTL_SECONDS = 4 * 60 * 60


@contextmanager
def _drive_upload_session_lock(task: str, kind: str, filename: str):
    """Serialize creation of one resumable session without locking other files."""
    lock_name = hashlib.sha256(f"{task}\0{kind}\0{filename}".encode("utf-8")).hexdigest()
    lock_path = index_path(task).parent / ".upload-sessions" / f"{lock_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - local Windows development
            yield
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _oauth_credentials(settings: Settings) -> tuple[str, str]:
    client_id = (os.getenv("GOOGLE_DRIVE_CLIENT_ID") or getattr(settings, "youtube_client_id", None) or os.getenv("YOUTUBE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_DRIVE_CLIENT_SECRET") or getattr(settings, "youtube_client_secret", None) or os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "Configure primeiro o Client ID e o Client Secret OAuth do Google nas Configurações.")
    return client_id, client_secret


def _callback_uri(request: Request) -> str:
    return (os.getenv("GOOGLE_DRIVE_OAUTH_REDIRECT_URI") or str(request.url_for("google_drive_oauth_callback"))).strip()


@router.get("/status")
def google_drive_status(current_user: User = Depends(get_current_admin_user)):
    archive = GoogleDriveArchive(user_id=current_user.id)
    try:
        if not archive.is_configured():
            return {"connected": False, "folder": ROOT_FOLDER_NAME}
        return archive.status()
    except GoogleDriveArchiveError as exc:
        return {"connected": False, "folder": ROOT_FOLDER_NAME, "error": str(exc)}


@router.get("/accounts")
def google_drive_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    GoogleDriveArchive._ensure_legacy_account(db)
    accounts = db.query(GoogleDriveAccount).order_by(
        GoogleDriveAccount.is_primary.desc(), GoogleDriveAccount.id.asc()
    ).all()
    return {
        "connected": bool(accounts) or GoogleDriveArchive(user_id=current_user.id).is_configured(),
        "enabled_accounts": sum(1 for item in accounts if item.enabled),
        "primary_id": next((item.id for item in accounts if item.is_primary), None),
        "accounts": [
            {
                "id": item.id,
                "label": item.label,
                "connected": bool(item.refresh_token),
                "enabled": bool(item.enabled),
                "is_primary": bool(item.is_primary),
                "is_legacy": bool(item.is_legacy),
            }
            for item in accounts
        ],
    }


@router.post("/accounts/{account_id}/primary")
def set_google_drive_primary(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    account = db.query(GoogleDriveAccount).filter(GoogleDriveAccount.id == int(account_id)).first()
    if account is None:
        raise HTTPException(404, "Conta do Google Drive não encontrada.")
    for item in db.query(GoogleDriveAccount).all():
        item.is_primary = item.id == account.id
    account.enabled = True
    db.commit()
    return {"ok": True, "primary_id": account.id}


@router.patch("/accounts/{account_id}")
async def update_google_drive_account(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    account = db.query(GoogleDriveAccount).filter(GoogleDriveAccount.id == int(account_id)).first()
    if account is None:
        raise HTTPException(404, "Conta do Google Drive não encontrada.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(400, "Dados da conta inválidos.")
    if "label" in payload:
        label = str(payload.get("label") or "").strip()[:120]
        if not label:
            raise HTTPException(400, "Informe um nome para a conta.")
        account.label = label
    if "enabled" in payload:
        enabled = bool(payload.get("enabled"))
        if not enabled and account.enabled:
            active_others = db.query(GoogleDriveAccount).filter(
                GoogleDriveAccount.enabled.is_(True), GoogleDriveAccount.id != account.id
            ).count()
            if active_others < 1:
                raise HTTPException(400, "Mantenha pelo menos uma conta ativa para novos projetos.")
            if account.is_primary:
                replacement = db.query(GoogleDriveAccount).filter(
                    GoogleDriveAccount.enabled.is_(True), GoogleDriveAccount.id != account.id
                ).order_by(GoogleDriveAccount.id.asc()).first()
                account.is_primary = False
                if replacement:
                    replacement.is_primary = True
        account.enabled = enabled
    db.commit()
    return {"ok": True, "id": account.id, "label": account.label, "enabled": bool(account.enabled)}


@router.get("/auth-url")
def google_drive_auth_url(
    request: Request,
    label: str = "",
    account_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    settings = _get_or_create_settings_row(db, user_id=current_user.id)
    GoogleDriveArchive._ensure_legacy_account(db)
    client_id, client_secret = _oauth_credentials(settings)
    existing_account = None
    if account_id is not None:
        existing_account = db.query(GoogleDriveAccount).filter(
            GoogleDriveAccount.id == int(account_id)
        ).first()
        if existing_account is None:
            raise HTTPException(404, "Conta do Google Drive não encontrada.")
    account_label = str(label or (existing_account.label if existing_account else "")).strip()[:120]
    if not account_label:
        raise HTTPException(400, "Informe um nome para identificar esta conta do Google Drive.")
    redirect_uri = _callback_uri(request)
    if not redirect_uri.startswith("https://") and "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        raise HTTPException(400, "O Google Drive exige callback HTTPS. Configure GOOGLE_DRIVE_OAUTH_REDIRECT_URI com a URL HTTPS pública do app.")
    state = _STATE.dumps({
        "user_id": int(current_user.id), "redirect_uri": redirect_uri,
        "account_id": int(existing_account.id) if existing_account else None,
        "label": account_label, "nonce": uuid.uuid4().hex,
    })
    client_config = {"web": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect_uri],
    }}
    try:
        flow = Flow.from_client_config(client_config, scopes=[DRIVE_SCOPE], state=state)
        flow.redirect_uri = redirect_uri
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent select_account", include_granted_scopes="true", state=state,
        )
    except Exception as exc:
        raise HTTPException(400, "Não foi possível iniciar a autorização do Google Drive. Verifique o OAuth Client e o callback cadastrado.") from exc
    return {"auth_url": auth_url, "redirect_uri": redirect_uri}


@router.get("/callback", name="google_drive_oauth_callback", response_class=HTMLResponse)
def google_drive_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    if error:
        return HTMLResponse(f"<h2>Google Drive não conectado</h2><p>{escape(error)}</p><p>Feche esta aba e tente novamente nas Configurações.</p>", status_code=400)
    try:
        state_data = _STATE.loads(state, max_age=600)
        user_id = int(state_data["user_id"])
        redirect_uri = str(state_data["redirect_uri"])
        account_id = state_data.get("account_id")
        account_label = str(state_data.get("label") or "Conta Google Drive").strip()[:120]
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return HTMLResponse("<h2>Autorização expirada</h2><p>Feche esta aba e conecte o Google Drive novamente nas Configurações.</p>", status_code=400)
    if not code:
        return HTMLResponse("<h2>Código Google ausente</h2><p>Feche esta aba e tente conectar novamente.</p>", status_code=400)
    settings = _get_or_create_settings_row(db, user_id=user_id)
    try:
        client_id, client_secret = _oauth_credentials(settings)
        client_config = {"web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }}
        flow = Flow.from_client_config(client_config, scopes=[DRIVE_SCOPE], state=state)
        flow.redirect_uri = redirect_uri
        flow.fetch_token(code=code)
        credentials = flow.credentials
        account = None
        if account_id is not None:
            account = db.query(GoogleDriveAccount).filter(
                GoogleDriveAccount.id == int(account_id)
            ).first()
            if account is None:
                raise RuntimeError("A conta escolhida para reconexão não existe mais.")
        if credentials.refresh_token:
            encrypted_token = encrypt_refresh_token(credentials.refresh_token)
        elif account and account.refresh_token:
            encrypted_token = account.refresh_token
        else:
            raise RuntimeError("O Google não retornou refresh token. Refaça a autorização com consentimento.")

        if account is None:
            account = GoogleDriveAccount(
                label=account_label,
                refresh_token=encrypted_token,
                enabled=True,
                is_primary=False,
                is_legacy=False,
            )
            if db.query(GoogleDriveAccount).count() == 0:
                account.is_primary = True
                account.is_legacy = True
            db.add(account)
        else:
            account.label = account_label
            account.refresh_token = encrypted_token
            account.folder_id = None
        # Keep legacy fields readable by older workers until they are updated.
        settings.google_drive_refresh_token = encrypted_token
        settings.google_drive_folder_id = None
        db.add(settings)
        db.commit()
        db.refresh(account)
        try:
            GoogleDriveArchive(account_id=account.id).root_folder_id()
        except GoogleDriveArchiveError:
            # The account is saved; the root folder can be created on first upload.
            pass
        from app.services.server_resource_monitor import invalidate_drive_usage_cache

        invalidate_drive_usage_cache(user_id)
    except Exception as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, (RuntimeError, GoogleDriveArchiveError)) else "Falha ao trocar o código OAuth. Confira o callback HTTPS cadastrado no Google Cloud."
        return HTMLResponse(f"<h2>Falha ao conectar Google Drive</h2><p>{escape(message)}</p><p>Feche esta aba e revise as Configurações do Google OAuth.</p>", status_code=400)
    return HTMLResponse(
        "<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Google Drive conectado</title><body style='font:16px system-ui;max-width:640px;margin:12vh auto;padding:24px;color:#172033'>"
        f"<h2>Google Drive conectado</h2><p>A conta <b>{escape(account_label)}</b> foi conectada. Os próximos arquivos serão enviados pelo CX33 diretamente ao Drive.</p>"
        "<p>Feche esta aba e atualize o status nas Configurações. Projetos antigos continuam associados à conta em que foram arquivados.</p></body></html>"
    )


@router.post("/{task}/upload-session", include_in_schema=False)
async def google_drive_upload_session(task: str, request: Request):
    # Authentication is applied explicitly because this internal endpoint uses
    # the machine-to-machine transfer token, not a browser session.
    from app.routers.production_assets import authenticate
    authenticate(request.headers.get("X-Codexia-Asset-Token", ""))
    try:
        task = task_key(task)
        payload = await request.json()
        kind = str(payload.get("kind") or "")
        filename = str(payload.get("filename") or "")
        sha256 = str(payload.get("sha256") or "").strip().lower()
        size = int(payload.get("size") or 0)
        expected_previous_sha256 = str(payload.get("expected_previous_sha256") or "").strip().lower()
    except Exception as exc:
        raise HTTPException(400, "Solicitação de upload inválida.") from exc
    if size > int(os.getenv("CODEXIA_ASSET_MAX_BYTES", str(4 * 1024**3))):
        raise HTTPException(413, "Ativo excede o limite de transferência direta ao Google Drive.")
    try:
        from app.services.production_manifest import _atomic_write_json, _read_json
        session_key = f"{kind}/{filename}"
        with _drive_upload_session_lock(task, kind, filename):
            with task_index_lock(task):
                index_file = index_path(task)
                index = _read_json(index_file)
                pending_uploads = index.get("pending_uploads") or {}
                pending = pending_uploads.get(session_key)
                if pending:
                    created_at = float(pending.get("created_at") or 0)
                    if time.time() - created_at < _UPLOAD_SESSION_TTL_SECONDS:
                        if (pending.get("sha256") == sha256
                                and int(pending.get("size") or -1) == size):
                            upload_url = decrypt_drive_secret(pending.get("upload_url") or "")
                            if not upload_url.startswith("https://www.googleapis.com/upload/drive/v3/files"):
                                raise HTTPException(503, "A sessão retomável salva para este ativo não é válida.")
                            return {
                                "already_present": False,
                                "upload_url": upload_url,
                                "drive_account_id": pending.get("drive_account_id"),
                                "kind": kind,
                                "filename": filename,
                                "sha256": sha256,
                                "size": size,
                                "resumed": True,
                            }
                        raise HTTPException(status_code=409, detail={
                            "code": "ASSET_UPLOAD_IN_PROGRESS",
                            "message": "Outro envio deste ativo ainda está em andamento. O arquivo local foi preservado.",
                        })
                    pending_uploads.pop(session_key, None)
                    index["pending_uploads"] = pending_uploads
                    _atomic_write_json(index_file, index)

            drive = GoogleDriveArchive.for_task(task)
            values = drive._settings()
            account_id = values.get("account_id")
            if account_id is not None:
                pin_task_drive_account(task, account_id)
            result = drive.create_upload_session(
                task, kind, filename, sha256, size, expected_previous_sha256
            )
            if result.get("already_present"):
                entry = result["entry"]
                register_asset(task, entry, expected_previous_sha256=expected_previous_sha256)
                return {"already_present": True, "entry": entry}
            with task_index_lock(task):
                index = _read_json(index_file)
                pending_uploads = index.get("pending_uploads") or {}
                pending_uploads[session_key] = {
                    "sha256": sha256,
                    "size": size,
                    "drive_account_id": result.get("drive_account_id"),
                    "upload_url": encrypt_drive_secret(result["upload_url"]),
                    "expected_previous_sha256": expected_previous_sha256,
                    "created_at": time.time(),
                }
                index["pending_uploads"] = pending_uploads
                _atomic_write_json(index_file, index)
            return result
    except DriveAssetConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "ASSET_CONTENT_CONFLICT", "existing_sha256": exc.existing_sha256,
            "message": str(exc),
        })
    except GoogleDriveArchiveError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/{task}/access", include_in_schema=False)
def google_drive_task_access(
    task: str,
    request: Request,
):
    from app.routers.production_assets import authenticate
    authenticate(request.headers.get("X-Codexia-Asset-Token", ""))
    try:
        task = task_key(task)
        drive = GoogleDriveArchive.for_task(task)
        values = drive._settings()
        account_id = values.get("account_id")
        if account_id is not None:
            pin_task_drive_account(task, account_id)
        credentials = drive.credentials()
        return {
            "access_token": credentials.token,
            "drive_account_id": account_id,
            "expires_at": credentials.expiry.isoformat() if credentials.expiry else None,
        }
    except GoogleDriveArchiveError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{task}/register", include_in_schema=False)
async def google_drive_register_asset(task: str, request: Request):
    from app.routers.production_assets import authenticate
    authenticate(request.headers.get("X-Codexia-Asset-Token", ""))
    try:
        task = task_key(task)
        payload = await request.json()
        entry = {
            "kind": str(payload.get("kind") or ""),
            "filename": str(payload.get("filename") or ""),
            "drive_file_id": str(payload.get("drive_file_id") or "").strip(),
            "drive_account_id": payload.get("drive_account_id"),
            "sha256": str(payload.get("sha256") or "").strip().lower(),
            "size": int(payload.get("size") or 0),
        }
        if entry["drive_account_id"] not in (None, ""):
            entry["drive_account_id"] = int(entry["drive_account_id"])
        if not entry["drive_file_id"] or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or entry["size"] <= 0:
            raise ValueError("Metadados do ativo inválidos.")
        from app.services.central_asset_transfer import archive_path
        archive_path(entry["kind"], entry["filename"], task=task)
        pinned_id = pin_task_drive_account(task, entry["drive_account_id"]) if entry["drive_account_id"] else None
        if pinned_id is not None and pinned_id != entry["drive_account_id"]:
            raise ValueError("A conta do ativo não corresponde à conta vinculada ao projeto.")
        metadata = GoogleDriveArchive.for_asset(entry).get_metadata(entry["drive_file_id"])
        properties = metadata.get("appProperties") or {}
        if (int(metadata.get("size") or -1) != entry["size"]
                or properties.get("sha256") != entry["sha256"]
                or properties.get("task") != task
                or properties.get("kind") != entry["kind"]):
            raise ValueError("Google Drive não confirmou a integridade e o vínculo deste arquivo.")
        register_asset(task, entry)
        from app.services.server_resource_monitor import invalidate_drive_usage_cache
        invalidate_drive_usage_cache()
        return {"registered": True, "entry": entry}
    except GoogleDriveArchiveError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _migration_dir() -> Path:
    root = Path(os.getenv("CODEXIA_GOOGLE_DRIVE_MIGRATION_DIR") or "/data/media/google_drive_migrations")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "")):
        raise HTTPException(404, "Migração não encontrada.")
    return _migration_dir() / f"{job_id}.json"


def _write_job(job_id: str, payload: Dict[str, Any]) -> None:
    path = _job_path(job_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_job(job_id: str) -> Dict[str, Any]:
    path = _job_path(job_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, "Migração não encontrada.")
    except Exception:
        raise HTTPException(500, "Não foi possível ler o estado da migração.")


def _timestamp(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, datetime):
        try:
            normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return normalized.timestamp()
        except Exception:
            return fallback
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except Exception:
            pass
    return fallback


def _local_archive_path(raw: Any, allowed_roots: list[Path]) -> Optional[Path]:
    value = str(raw or "").strip()
    if not value:
        return None
    path = Path(value)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        resolved = path.resolve(strict=True)
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def _project_archive_inventory():
    """Index terminal project assets and every manifest-known hard-link alias."""
    from app.services.production_manifest import _read_json, _root_dir
    from app.services.task_manager import get_task

    manifest_root = Path(_root_dir()).resolve()
    allowed_roots = [Path(value).resolve() for value in roots().values()]
    allowed_roots.append(manifest_root)
    cutoff = time.time() - 24 * 60 * 60
    projects: Dict[str, Dict[str, Any]] = {}
    inodes: Dict[tuple[int, int], Dict[str, Any]] = {}

    for manifest_path in manifest_root.glob("*/manifest.json"):
        task_id = manifest_path.parent.name
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            continue
        manifest = _read_json(manifest_path)
        try:
            task = get_task(task_id) or {}
        except Exception:
            task = {}
        status = str(task.get("status") or manifest.get("status") or "").strip().lower()
        try:
            manifest_mtime = manifest_path.stat().st_mtime
        except OSError:
            manifest_mtime = 0.0
        updated_at = _timestamp(task.get("updated_at") or manifest.get("updated_at"), manifest_mtime)

        project = {
            "task_id": task_id,
            "status": status,
            "updated_at": updated_at,
            "manifest_path": manifest_path,
            "script_path": manifest_path.parent / "script.json",
            "assets": {},
        }
        for artifact in manifest.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            kind = str(artifact.get("kind") or "").strip().lower()
            if kind not in _MIGRATION_EXTENSIONS:
                continue
            aliases = []
            for key in ("durable_path", "original_path"):
                candidate = _local_archive_path(artifact.get(key), allowed_roots)
                if candidate and candidate not in aliases:
                    aliases.append(candidate)
            if not aliases:
                continue
            source = aliases[0]
            original = str(artifact.get("original_path") or source)
            filename = Path(original).name
            if not filename or Path(filename).suffix.lower() not in _MIGRATION_EXTENSIONS[kind]:
                continue
            ref_key = (kind, filename)
            project["assets"].setdefault(ref_key, {"kind": kind, "filename": filename, "path": source})
            for alias in aliases:
                try:
                    info = alias.stat()
                except OSError:
                    continue
                inode_key = (int(info.st_dev), int(info.st_ino))
                group = inodes.setdefault(inode_key, {
                    "device": inode_key[0], "inode": inode_key[1],
                    "size": int(info.st_size), "link_count": int(info.st_nlink),
                    "paths": set(), "owners": {},
                })
                group["paths"].add(alias)
                group["link_count"] = max(group["link_count"], int(info.st_nlink))
                group["owners"].setdefault(task_id, set()).add(ref_key)

        projects[task_id] = project

    candidates = [
        project for project in projects.values()
        if project["status"] in _ARCHIVABLE_STATUSES
        and project["updated_at"] <= cutoff
        and (project["assets"] or project["manifest_path"].is_file())
    ]
    candidates.sort(key=lambda item: item["updated_at"] or 0.0)
    return projects, inodes, candidates


def _drive_index_for_task(task_id: str) -> Dict[tuple[str, str], Dict[str, Any]]:
    from app.services.production_manifest import _read_json

    try:
        payload = _read_json(index_path(task_id))
        return {
            (str(item.get("kind") or ""), str(item.get("filename") or "")): item
            for item in payload.get("files") or []
            if isinstance(item, dict) and item.get("drive_file_id")
        }
    except Exception:
        return {}


def _remove_fully_archived_aliases(projects: Dict[str, Dict[str, Any]], inodes: Dict[tuple[int, int], Dict[str, Any]]) -> int:
    """Remove only known links after every owning task has a matching Drive copy."""
    from app.services.production_manifest import _root_dir

    manifest_root = Path(_root_dir()).resolve()
    allowed_roots = [Path(value).resolve() for value in roots().values()]
    allowed_roots.append(manifest_root)
    index_cache: Dict[str, Dict[tuple[str, str], Dict[str, Any]]] = {}
    freed_before = shutil.disk_usage("/data" if os.path.isdir("/data") else "/").free
    cutoff = time.time() - 24 * 60 * 60

    for group in inodes.values():
        owners = group["owners"]
        if not owners or len(group["paths"]) != int(group["link_count"] or 0):
            continue
        if any(
            projects.get(task_id, {}).get("status") not in _ARCHIVABLE_STATUSES
            or float(projects.get(task_id, {}).get("updated_at") or 0.0) > cutoff
            for task_id in owners
        ):
            continue
        existing_paths = [path for path in group["paths"] if path.is_file() and not path.is_symlink()]
        if not existing_paths:
            continue
        try:
            info = existing_paths[0].stat()
            if ((int(info.st_dev), int(info.st_ino)) != (group["device"], group["inode"])
                    or int(info.st_nlink) != int(group["link_count"] or 0)
                    or len(existing_paths) != int(info.st_nlink)):
                continue
            expected_sha = digest(existing_paths[0])
            expected_size = int(info.st_size)
        except OSError:
            continue

        complete = True
        for task_id, refs in owners.items():
            if task_id not in index_cache:
                index_cache[task_id] = _drive_index_for_task(task_id)
            index = index_cache[task_id]
            for ref in refs:
                entry = index.get(ref) or {}
                if (str(entry.get("sha256") or "") != expected_sha
                        or int(entry.get("size") or -1) != expected_size):
                    complete = False
                    break
            if not complete:
                break
        if not complete:
            continue

        for path in existing_paths:
            try:
                resolved = path.resolve(strict=True)
                if any(resolved.is_relative_to(root) for root in allowed_roots):
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    freed_after = shutil.disk_usage("/data" if os.path.isdir("/data") else "/").free
    return max(0, int(freed_after - freed_before))


def _run_migration(job_id: str, user_id: int, limit: int) -> None:
    payload = _read_job(job_id)
    payload.update({"status": "processing", "started_at": datetime.now(timezone.utc).isoformat()})
    _write_job(job_id, payload)
    archived = 0
    skipped = 0
    errors = []
    bytes_freed = 0
    archived_projects = 0
    try:
        projects, inodes, candidates = _project_archive_inventory()
        selected = candidates[:limit]
        payload["total_candidates"] = len(selected)
        _write_job(job_id, payload)
        for project in selected:
            task_id = project["task_id"]
            archive = GoogleDriveArchive.for_task(task_id, user_id=user_id)
            account_id = archive._settings().get("account_id")
            if account_id is not None:
                pin_task_drive_account(task_id, account_id)
            assets = dict(project["assets"])
            for path, filename in (
                (project["manifest_path"], f"{task_id}-manifest.json"),
                (project["script_path"], f"{task_id}-script.json"),
            ):
                if path.is_file():
                    assets.setdefault(("script", filename), {"kind": "script", "filename": filename, "path": path})
            project_ok = bool(assets)
            try:
                for asset in assets.values():
                    path = Path(asset["path"])
                    try:
                        source_sha = digest(path)
                        source_size = path.stat().st_size
                        entry = archive.upload_path(task_id, asset["kind"], asset["filename"], path)
                        if digest(path) != source_sha or int(entry.get("size") or -1) != source_size or entry.get("sha256") != source_sha:
                            raise RuntimeError("A cópia local mudou ou o Drive não confirmou o mesmo conteúdo.")
                        register_asset(task_id, entry)
                        archived += 1
                    except Exception as exc:
                        project_ok = False
                        errors.append(f"{task_id}/{asset['filename']}: {str(exc)[:180]}")
                if project_ok:
                    archived_projects += 1
            except Exception as exc:
                errors.append(f"{task_id}: {str(exc)[:200]}")
            payload.update({
                "archived": archived,
                "archived_projects": archived_projects,
                "skipped": skipped,
                "bytes_freed": bytes_freed,
                "errors": errors[-10:],
            })
            _write_job(job_id, payload)
        # This sweep sees shared hard links across projects. It removes them only
        # after each owning project has a checksum-verified Drive entry.
        bytes_freed = _remove_fully_archived_aliases(projects, inodes)
    except Exception as exc:
        errors.append(str(exc)[:240])
    payload.update({
        "status": "completed" if not errors else ("completed_with_errors" if archived or skipped else "failed"),
        "archived": archived,
        "archived_projects": archived_projects,
        "skipped": skipped,
        "bytes_freed": bytes_freed,
        "errors": errors[-10:],
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    _write_job(job_id, payload)
    from app.services.server_resource_monitor import invalidate_drive_usage_cache

    invalidate_drive_usage_cache(user_id)


@router.post("/migrate/start")
def start_google_drive_migration(
    background_tasks: BackgroundTasks,
    limit: int = 5,
    current_user: User = Depends(get_current_admin_user),
):
    archive = GoogleDriveArchive(user_id=current_user.id)
    try:
        configured = archive.is_configured()
    except GoogleDriveArchiveError as exc:
        raise HTTPException(503, str(exc))
    if not configured:
        raise HTTPException(400, "Conecte o Google Drive antes de iniciar a migração.")
    limit = max(1, min(25, int(limit)))
    job_id = uuid.uuid4().hex
    _write_job(job_id, {
        "job_id": job_id, "status": "queued", "requested_limit": limit,
        "archived": 0, "archived_projects": 0, "skipped": 0, "bytes_freed": 0, "errors": [],
    })
    background_tasks.add_task(_run_migration, job_id, int(current_user.id), limit)
    return {"job_id": job_id, "status": "queued", "message": "Arquivamento por projeto iniciado. Tarefas ativas e projetos atualizados nas últimas 24 horas serão ignorados."}


@router.get("/migrate/{job_id}")
def google_drive_migration_status(job_id: str, current_user: User = Depends(get_current_admin_user)):
    return _read_job(job_id)
