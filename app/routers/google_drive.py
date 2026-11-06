"""OAuth connection and controlled migration to the Google Drive archive."""
from __future__ import annotations

import json
import hashlib
import hmac
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.database import SessionLocal, get_db
from app.models import GoogleDriveAccount, Settings, User
from app.routers.auth import SECRET_KEY, get_current_admin_user, get_current_user
from app.routers.settings import _get_or_create_settings_row
from app.services.central_asset_transfer import (
    cleanup_confirmed_worker_cache_job, digest, index_path, preserves_final_video,
    pin_task_drive_account, register as register_asset, roots,
    task_index_lock, task_key,
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
    google_drive_oauth_client_credentials,
)

router = APIRouter(prefix="/settings/google-drive", tags=["Google Drive archive"])
logger = logging.getLogger(__name__)
_STATE = URLSafeTimedSerializer(SECRET_KEY, salt="codexia-google-drive-oauth-v1")
_DOWNLOAD_STATE = URLSafeTimedSerializer(SECRET_KEY, salt="codexia-google-drive-download-v1")
_ARCHIVABLE_STATUSES = {
    "completed", "awaiting_review", "approved", "rendered_upload_failed",
    "failed", "paused", "cancelled", "published",
}
_MIGRATION_EXTENSIONS = {
    "video": {".mp4", ".mov", ".mkv", ".webm"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    "image": {".png", ".jpg", ".jpeg", ".webp"},
    "caption": {".srt", ".vtt", ".ass"},
}
_UPLOAD_SESSION_TTL_SECONDS = 4 * 60 * 60
_DOWNLOAD_TOKEN_TTL_SECONDS = 10 * 60
_MIGRATION_BATCH_SIZE = 10
_MIGRATION_BATCH_REFERENCE_SECONDS = 30 * 60


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
    try:
        client_id, client_secret = google_drive_oauth_client_credentials(settings)
    except GoogleDriveArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not client_id or not client_secret:
        raise HTTPException(
            400,
            "Configure o Client ID e o Client Secret exclusivos do Google Drive em Configurações.",
        )
    return client_id, client_secret


def _callback_uri(request: Request) -> str:
    return (os.getenv("GOOGLE_DRIVE_OAUTH_REDIRECT_URI") or str(request.url_for("google_drive_oauth_callback"))).strip()


def _oauth_pair_fingerprint(client_id: str, client_secret: str) -> str:
    """Return a keyed, short fingerprint so OAuth attempts can be compared without logging secrets."""
    key = str(SECRET_KEY).encode("utf-8")
    material = f"{client_id}\0{client_secret}".encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()[:12]


def _oauth_error_reason(exc: Exception) -> str:
    """Map Google's description to a fixed label; never log provider-supplied text."""
    description = getattr(exc, "description", None)
    if not isinstance(description, str) or not description.strip():
        for arg in getattr(exc, "args", ()):
            if isinstance(arg, dict) and isinstance(arg.get("error_description"), str):
                description = arg["error_description"]
                break
    if not isinstance(description, str) or not description.strip():
        return "unavailable"
    normalized = re.sub(r"[^a-z0-9]+", " ", description.lower()).strip()
    if "missing code verifier" in normalized or "missing code_verifier" in description.lower():
        return "pkce_verifier_missing"
    if "token has been expired or revoked" in normalized:
        return "token_expired_or_revoked"
    if "redirect" in normalized and "uri" in normalized:
        return "redirect_uri_rejected"
    if "client" in normalized and any(word in normalized for word in ("invalid", "deleted", "unauthorized")):
        return "client_rejected"
    if "code" in normalized and any(word in normalized for word in ("invalid", "expired", "used")):
        return "authorization_code_rejected"
    if "bad request" in normalized:
        return "bad_request"
    return "other"


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
    flow_id = uuid.uuid4().hex[:12]
    client_fingerprint = _oauth_pair_fingerprint(client_id, client_secret)
    # PKCE requires the same verifier at authorization and token exchange.
    # Encrypt it because OAuth state is signed but otherwise readable in URLs.
    code_verifier = secrets.token_urlsafe(64)
    state = _STATE.dumps({
        "user_id": int(current_user.id), "redirect_uri": redirect_uri,
        "account_id": int(existing_account.id) if existing_account else None,
        "label": account_label, "nonce": uuid.uuid4().hex,
        "flow_id": flow_id, "issued_at": int(time.time()),
        "client_fingerprint": client_fingerprint,
        "code_verifier": encrypt_drive_secret(code_verifier),
    })
    client_config = {"web": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect_uri],
    }}
    try:
        flow = Flow.from_client_config(
            client_config, scopes=[DRIVE_SCOPE], state=state,
            code_verifier=code_verifier, autogenerate_code_verifier=False,
        )
        flow.redirect_uri = redirect_uri
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent select_account", include_granted_scopes="true", state=state,
        )
    except Exception as exc:
        raise HTTPException(400, "Não foi possível iniciar a autorização do Google Drive. Verifique o OAuth Client e o callback cadastrado.") from exc
    redirect_fingerprint = hashlib.sha256(redirect_uri.encode("utf-8")).hexdigest()[:12]
    logger.info(
        "Google Drive OAuth authorization started: flow_id=%s client_fingerprint=%s redirect_fingerprint=%s",
        flow_id, client_fingerprint, redirect_fingerprint,
    )
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
        flow_id = str(state_data.get("flow_id") or "legacy")[:32]
        state_client_fingerprint = str(state_data.get("client_fingerprint") or "")
        encrypted_code_verifier = str(state_data.get("code_verifier") or "")
        if not encrypted_code_verifier:
            raise ValueError("PKCE verifier missing from OAuth state")
        code_verifier = decrypt_drive_secret(encrypted_code_verifier)
        if not code_verifier:
            raise ValueError("PKCE verifier could not be decrypted")
        try:
            flow_age_seconds = max(0, int(time.time() - float(state_data["issued_at"])))
        except (KeyError, TypeError, ValueError, OverflowError):
            flow_age_seconds = "unavailable"
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError, GoogleDriveArchiveError):
        return HTMLResponse("<h2>Autorização expirada</h2><p>Feche esta aba e conecte o Google Drive novamente nas Configurações.</p>", status_code=400)
    if not code:
        return HTMLResponse("<h2>Código Google ausente</h2><p>Feche esta aba e tente conectar novamente.</p>", status_code=400)
    settings = _get_or_create_settings_row(db, user_id=user_id)
    oauth_stage = "load_client_credentials"
    try:
        client_id, client_secret = _oauth_credentials(settings)
        current_client_fingerprint = _oauth_pair_fingerprint(client_id, client_secret)
        callback_redirect_uri = _callback_uri(request)
        redirect_fingerprint = hashlib.sha256(redirect_uri.encode("utf-8")).hexdigest()[:12]
        callback_redirect_fingerprint = hashlib.sha256(callback_redirect_uri.encode("utf-8")).hexdigest()[:12]
        logger.info(
            "Google Drive OAuth callback received: flow_id=%s flow_age_seconds=%s "
            "credentials_match_state=%s callback_uri_matches_state=%s "
            "redirect_fingerprint=%s callback_redirect_fingerprint=%s",
            flow_id, flow_age_seconds,
            "yes" if state_client_fingerprint and hmac.compare_digest(state_client_fingerprint, current_client_fingerprint) else "no",
            "yes" if callback_redirect_uri == redirect_uri else "no",
            redirect_fingerprint, callback_redirect_fingerprint,
        )
        oauth_stage = "create_oauth_flow"
        client_config = {"web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }}
        flow = Flow.from_client_config(
            client_config, scopes=[DRIVE_SCOPE], state=state,
            code_verifier=code_verifier, autogenerate_code_verifier=False,
        )
        flow.redirect_uri = redirect_uri
        oauth_stage = "exchange_authorization_code"
        flow.fetch_token(code=code)
        credentials = flow.credentials
        oauth_stage = "persist_refresh_token"
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
        oauth_error = getattr(exc, "error", None)
        if not isinstance(oauth_error, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", oauth_error):
            oauth_error = "unavailable"
        response = getattr(exc, "response", None)
        http_status = getattr(response, "status_code", None)
        if not isinstance(http_status, int):
            http_status = getattr(exc, "status_code", None)
        if not isinstance(http_status, int):
            http_status = "unavailable"
        logger.warning(
            "Google Drive OAuth callback failed: flow_id=%s stage=%s exception_type=%s "
            "oauth_error=%s oauth_reason=%s http_status=%s",
            flow_id, oauth_stage, type(exc).__name__, oauth_error,
            _oauth_error_reason(exc), http_status,
        )
        message = str(exc) if isinstance(exc, (RuntimeError, GoogleDriveArchiveError)) else "Falha ao trocar o código OAuth. Confira as credenciais e o callback HTTPS do Google Cloud."
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


def _refresh_migration_timing(payload: Dict[str, Any], now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    now_seconds = now.timestamp()
    started_at = _timestamp(payload.get("started_at"), 0.0)
    if started_at > 0:
        payload["elapsed_seconds"] = max(0, int(now_seconds - started_at))

    batch_started_at = _timestamp(payload.get("batch_started_at"), 0.0)
    if batch_started_at > 0:
        payload["batch_elapsed_seconds"] = max(0, int(now_seconds - batch_started_at))

    history = payload.get("batch_duration_history") or []
    samples = [
        (max(0.0, float(item.get("seconds") or 0)), max(0, int(item.get("projects") or 0)))
        for item in history
        if isinstance(item, dict) and int(item.get("projects") or 0) > 0
    ]
    total_sample_projects = sum(projects for _seconds, projects in samples)
    if total_sample_projects:
        average_project_seconds = sum(seconds for seconds, _projects in samples) / total_sample_projects
        payload["average_batch_seconds"] = int(round(average_project_seconds * _MIGRATION_BATCH_SIZE))
        total_projects = int(payload.get("overall_total_projects") or 0)
        overall_done = int(payload.get("overall_processed_projects") or 0)
        baseline = int(payload.get("stage_baseline_projects") or payload.get("batch_baseline_projects") or 0)
        batch_total = int(payload.get("batch_project_total") or 0)
        batch_done = max(0, min(batch_total, overall_done - baseline))
        payload["estimated_batch_remaining_seconds"] = max(
            0, int(round(average_project_seconds * max(0, batch_total - batch_done)))
        ) if batch_total else None
        payload["estimated_remaining_seconds"] = max(
            0, int(round(average_project_seconds * max(0, total_projects - overall_done)))
        ) if total_projects else None
    elif int(payload.get("batch_size") or 0):
        payload["average_batch_seconds"] = None
        payload["estimated_batch_remaining_seconds"] = None
        payload["estimated_remaining_seconds"] = None
    else:
        total_bytes = int(payload.get("overall_total_bytes") or 0)
        archived_bytes = int(payload.get("overall_archived_bytes") or 0)
        current_size = int(payload.get("current_asset_size_bytes") or 0)
        current_uploaded = min(current_size, int(payload.get("current_asset_uploaded_bytes") or 0))
        effective_bytes = min(total_bytes, archived_bytes + current_uploaded) if total_bytes > 0 else 0
        elapsed = int(payload.get("elapsed_seconds") or 0)
        if total_bytes > 0 and effective_bytes > 0 and elapsed > 0:
            rate = effective_bytes / elapsed
            payload["estimated_remaining_seconds"] = max(0, int((total_bytes - effective_bytes) / rate)) if rate > 0 else None
        elif total_bytes <= 0:
            payload["estimated_remaining_seconds"] = None


def _write_job(job_id: str, payload: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    _refresh_migration_timing(payload, now)
    payload["status_updated_at"] = now.isoformat()
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


def _local_archive_path(
    raw: Any,
    allowed_roots: list[Path],
    path_cache: Optional[Dict[str, Optional[Path]]] = None,
    stat_cache: Optional[Dict[str, os.stat_result]] = None,
) -> Optional[Path]:
    value = str(raw or "").strip()
    if not value:
        return None
    if path_cache is not None and value in path_cache:
        return path_cache[value]
    path = Path(value)
    resolved = None
    try:
        # One lstat replaces separate is_symlink/is_file checks. The resolved
        # path is still constrained to managed roots before it can be archived.
        info = path.lstat()
        import stat as stat_module
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
            if path_cache is not None:
                path_cache[value] = None
            return None
        resolved = path.resolve(strict=True)
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            resolved = None
        elif stat_cache is not None:
            stat_cache[str(resolved)] = info
    except (OSError, RuntimeError):
        resolved = None
    if path_cache is not None:
        path_cache[value] = resolved
    return resolved


def _saved_music_archive_projects() -> list[Dict[str, Any]]:
    """Inventory Suno/MusicGen outputs and lyrics without marking them for deletion."""
    from app.config import COVERS_OUTPUT_DIR, MUSIC_OUTPUT_DIR, VIDEO_OUTPUT_DIR
    from app.models import SavedMusic, SavedMusicShort

    db = SessionLocal()
    projects = []
    try:
        items = db.query(SavedMusic).order_by(SavedMusic.id.asc()).all()
        allowed_roots = [Path(value).resolve() for value in roots().values()]
        allowed_roots.extend(Path(value).resolve() for value in (
            COVERS_OUTPUT_DIR, MUSIC_OUTPUT_DIR, VIDEO_OUTPUT_DIR,
        ))
        projects_by_music_id = {}
        for item in items:
            task_id = f"music-{int(item.id)}"
            assets = {}
            sources = (
                ("audio", (getattr(item, "music_filename", None), getattr(item, "music_url", None)), MUSIC_OUTPUT_DIR),
                ("audio", (getattr(item, "hq_wav_filename", None), getattr(item, "hq_wav_url", None)), MUSIC_OUTPUT_DIR),
                ("image", (getattr(item, "cover_filename", None), getattr(item, "cover_url", None)), COVERS_OUTPUT_DIR),
                ("video", (getattr(item, "clip_filename", None), getattr(item, "clip_url", None)), VIDEO_OUTPUT_DIR),
            )
            for kind, values, root in sources:
                raw = next((str(value).strip() for value in values if str(value or "").strip()), "")
                if not raw:
                    continue
                filename = Path(raw.replace("\\", "/").split("?", 1)[0].split("#", 1)[0]).name
                if not filename or Path(filename).suffix.lower() not in _MIGRATION_EXTENSIONS.get(kind, set()):
                    continue
                path = _local_archive_path(Path(root) / filename, allowed_roots)
                if path:
                    assets.setdefault((kind, filename), {"kind": kind, "filename": filename, "path": path})
            metadata_filename = f"{task_id}-lyrics.json"
            metadata = json.dumps({
                "saved_music_id": int(item.id),
                "title": str(getattr(item, "title", None) or "Música"),
                "lyrics": str(getattr(item, "lyrics", None) or ""),
                "genre": str(getattr(item, "genre", None) or ""),
                "vocal_gender": str(getattr(item, "vocal_gender", None) or ""),
                "with_vocals": bool(getattr(item, "with_vocals", False)),
                "created_at": getattr(item, "created_at", None).isoformat() if getattr(item, "created_at", None) else None,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            assets[("script", metadata_filename)] = {
                "kind": "script", "filename": metadata_filename, "content": metadata,
            }
            project = {
                "task_id": task_id,
                "status": "completed",
                "updated_at": _timestamp(getattr(item, "updated_at", None) or getattr(item, "created_at", None)),
                "manifest_path": None,
                "script_path": None,
                "assets": assets,
                "preserve_local": True,
            }
            projects.append(project)
            projects_by_music_id[int(item.id)] = project

        # Music clips are stored in their own table, so include them in the
        # parent song's archive and lookup button as well.
        shorts = db.query(SavedMusicShort).order_by(SavedMusicShort.id.asc()).all()
        for short in shorts:
            parent_id = int(getattr(short, "parent_saved_music_id", 0) or 0)
            project = projects_by_music_id.get(parent_id)
            if project is None:
                continue
            raw = str(getattr(short, "clip_filename", None) or getattr(short, "clip_url", None) or "").strip()
            if raw:
                filename = Path(raw.replace("\\", "/").split("?", 1)[0].split("#", 1)[0]).name
                if filename and Path(filename).suffix.lower() in _MIGRATION_EXTENSIONS["video"]:
                    path = _local_archive_path(Path(VIDEO_OUTPUT_DIR) / filename, allowed_roots)
                    if path:
                        project["assets"].setdefault(
                            ("video", filename),
                            {"kind": "video", "filename": filename, "path": path},
                        )
            metadata_filename = f"music-{parent_id}-short-{int(short.id)}.json"
            metadata = json.dumps({
                "saved_music_id": parent_id,
                "saved_music_short_id": int(short.id),
                "title": str(getattr(short, "title", None) or "Clipe curto"),
                "start_sec": getattr(short, "start_sec", None),
                "end_sec": getattr(short, "end_sec", None),
                "youtube_video_id": str(getattr(short, "youtube_video_id", None) or ""),
                "created_at": getattr(short, "created_at", None).isoformat() if getattr(short, "created_at", None) else None,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            project["assets"][("script", metadata_filename)] = {
                "kind": "script", "filename": metadata_filename, "content": metadata,
            }
    finally:
        db.close()
    return projects


def _versioned_archive_filename(filename: str, sha256: str) -> str:
    """Give conflicting historical contents an immutable name in Drive."""
    path = Path(filename)
    suffix = path.suffix
    marker = f"__sha256-{sha256}"
    max_stem = 240 - len(suffix) - len(marker)
    return f"{path.stem[:max_stem]}{marker}{suffix}"


def _project_needs_archive(
    project: Dict[str, Any],
    digest_cache: Optional[Dict[tuple, str]] = None,
    progress_callback=None,
) -> bool:
    """Skip a project only when each current local asset has a verified index entry."""
    task_id = str(project.get("task_id") or "")
    index = _drive_index_for_task(task_id)
    assets = dict(project.get("assets") or {})
    for path, filename in (
        (project.get("manifest_path"), f"{task_id}-manifest.json"),
        (project.get("script_path"), f"{task_id}-script.json"),
    ):
        if path and Path(path).is_file():
            assets.setdefault(("script", filename), {"kind": "script", "filename": filename, "path": path})
    for (kind, filename), asset in assets.items():
        if "content" in asset:
            content = str(asset.get("content") or "").encode("utf-8")
            sha256 = hashlib.sha256(content).hexdigest()
            size = len(content)
        else:
            path = Path(asset["path"])
            if not path.is_file():
                continue
            # New archive entries cannot be skipped: avoid hashing large media
            # just to conclude that this project still needs an upload. The
            # upload path computes and verifies SHA-256 before Drive receives
            # bytes, and cleanup still requires a confirmed index entry.
            exact = index.get((kind, filename)) or {}
            if not exact:
                original = Path(filename)
                versioned_prefix = f"{original.stem}__sha256-"
                has_versioned_copy = any(
                    indexed_kind == kind
                    and indexed_name.startswith(versioned_prefix)
                    and indexed_name.endswith(original.suffix)
                    for indexed_kind, indexed_name in index
                )
                if not has_versioned_copy:
                    return True
            try:
                stat = path.stat()
                size = int(stat.st_size)
                digest_key = (int(stat.st_dev), int(stat.st_ino), size, int(stat.st_mtime_ns), int(stat.st_ctime_ns))
                sha256 = digest_cache.get(digest_key) if digest_cache is not None else None
                if not sha256:
                    if progress_callback:
                        progress_callback({
                            "phase": "verifying_inventory",
                            "task_id": task_id,
                            "asset": filename,
                            "asset_size_bytes": size,
                            "asset_verified_bytes": 0,
                        })
                    sha256 = digest(
                        path,
                        progress_callback=(
                            lambda processed, total: progress_callback({
                                "phase": "verifying_inventory",
                                "task_id": task_id,
                                "asset": filename,
                                "asset_size_bytes": int(total or size),
                                "asset_verified_bytes": int(processed),
                            })
                        ) if progress_callback else None,
                    )
                    if digest_cache is not None:
                        digest_cache[digest_key] = sha256
                elif progress_callback:
                    progress_callback({
                        "phase": "verifying_inventory",
                        "task_id": task_id,
                        "asset": filename,
                        "asset_size_bytes": size,
                        "asset_verified_bytes": size,
                    })
            except OSError:
                return True
        exact = index.get((kind, filename)) or {}
        if exact.get("sha256") == sha256 and int(exact.get("size") or -1) == size:
            continue
        versioned = index.get((kind, _versioned_archive_filename(filename, sha256))) or {}
        if versioned.get("sha256") == sha256 and int(versioned.get("size") or -1) == size:
            continue
        return True
    return False


def _migration_assets_for_project(project: Dict[str, Any]) -> Dict[tuple[str, str], Dict[str, Any]]:
    """Return the same local assets the migration worker will archive."""
    assets = dict(project.get("assets") or {})
    if not project.get("preserve_local"):
        task_id = str(project.get("task_id") or "")
        for path, filename in (
            (project.get("manifest_path"), f"{task_id}-manifest.json"),
            (project.get("script_path"), f"{task_id}-script.json"),
        ):
            if path and Path(path).is_file():
                assets.setdefault(("script", filename), {
                    "kind": "script", "filename": filename, "path": path,
                })
    return assets


def _migration_asset_size(asset: Dict[str, Any]) -> int:
    if "content" in asset:
        return len(str(asset.get("content") or "").encode("utf-8"))
    try:
        path = Path(asset.get("path") or "")
        return int(path.stat().st_size) if path.is_file() else 0
    except (OSError, TypeError, ValueError):
        return 0


def _project_archive_inventory(
    *,
    after_updated_at: Optional[float] = None,
    after_task_id: str = "",
    max_candidates: Optional[int] = None,
    progress_callback=None,
):
    """Index terminal project assets and every manifest-known hard-link alias."""
    from app.config import COVERS_OUTPUT_DIR
    from app.services.production_manifest import _read_json, _root_dir
    from app.models import VideoTask

    manifest_root = Path(_root_dir()).resolve()
    allowed_roots = [Path(value).resolve() for value in roots().values()]
    allowed_roots.append(Path(COVERS_OUTPUT_DIR).resolve())
    allowed_roots.append(manifest_root)
    cutoff = time.time() - 24 * 60 * 60
    projects: Dict[str, Dict[str, Any]] = {}
    inodes: Dict[tuple[int, int], Dict[str, Any]] = {}
    path_cache: Dict[str, Optional[Path]] = {}
    stat_cache: Dict[str, os.stat_result] = {}

    # Fetch terminal task metadata in one database round trip instead of
    # opening a SQLAlchemy session for every manifest in a large archive.
    manifest_paths = list(manifest_root.glob("*/manifest.json"))
    task_ids = [
        item.parent.name for item in manifest_paths
        if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item.parent.name)
    ]
    task_snapshots: Dict[str, Dict[str, Any]] = {}
    if task_ids:
        db = SessionLocal()
        try:
            rows = db.query(VideoTask.id, VideoTask.status, VideoTask.updated_at).filter(
                VideoTask.id.in_(task_ids)
            ).all()
            task_snapshots = {
                str(row.id): {"status": row.status, "updated_at": row.updated_at}
                for row in rows
            }
        except Exception:
            db.rollback()
            task_snapshots = {}
        finally:
            db.close()

    def scan_manifest(manifest_path: Path):
        task_id = manifest_path.parent.name
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            return None, {}
        manifest = _read_json(manifest_path)
        task = task_snapshots.get(task_id)
        # Preserve the legacy Redis/in-memory fallback only for tasks with no
        # row in the bulk database snapshot.
        if task is None:
            try:
                from app.services.task_manager import get_task
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
        local_path_cache: Dict[str, Optional[Path]] = {}
        local_stat_cache: Dict[str, os.stat_result] = {}
        local_inodes: Dict[tuple[int, int], Dict[str, Any]] = {}
        for artifact in manifest.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            kind = str(artifact.get("kind") or "").strip().lower()
            if kind not in _MIGRATION_EXTENSIONS:
                continue
            aliases = []
            for key in ("durable_path", "original_path"):
                candidate = _local_archive_path(
                    artifact.get(key), allowed_roots,
                    path_cache=local_path_cache, stat_cache=local_stat_cache,
                )
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
                    info = local_stat_cache.get(str(alias))
                    if info is None:
                        info = alias.stat()
                        local_stat_cache[str(alias)] = info
                except OSError:
                    continue
                inode_key = (int(info.st_dev), int(info.st_ino))
                group = local_inodes.setdefault(inode_key, {
                    "device": inode_key[0], "inode": inode_key[1],
                    "size": int(info.st_size), "link_count": int(info.st_nlink),
                    "paths": set(), "owners": {},
                })
                group["paths"].add(alias)
                group["link_count"] = max(group["link_count"], int(info.st_nlink))
                group["owners"].setdefault(task_id, set()).add(ref_key)
        return project, local_inodes

    manifests_scanned = 0
    scan_workers = min(2, len(manifest_paths)) if manifest_paths else 1
    with ThreadPoolExecutor(max_workers=scan_workers) as scan_pool:
        future_paths = {
            scan_pool.submit(scan_manifest, manifest_path): manifest_path
            for manifest_path in manifest_paths
        }
        for future in as_completed(future_paths):
            manifest_path = future_paths[future]
            project, local_inodes = future.result()
            manifests_scanned += 1
            if project:
                projects[str(project["task_id"])] = project
            for inode_key, local_group in local_inodes.items():
                group = inodes.get(inode_key)
                if group is None:
                    inodes[inode_key] = local_group
                    continue
                group["size"] = max(int(group.get("size") or 0), int(local_group.get("size") or 0))
                group["link_count"] = max(
                    int(group.get("link_count") or 0), int(local_group.get("link_count") or 0)
                )
                group["paths"].update(local_group["paths"])
                for task_id, refs in local_group["owners"].items():
                    group["owners"].setdefault(task_id, set()).update(refs)
            if progress_callback:
                progress_callback({
                    "phase": "inventory",
                    "task_id": manifest_path.parent.name,
                    "manifests_scanned": manifests_scanned,
                })

    for music_project in _saved_music_archive_projects():
        projects[music_project["task_id"]] = music_project

    digest_cache: Dict[tuple, str] = {}
    eligible_projects = [
        project for project in projects.values()
        if (
            project.get("preserve_local")
            or (project["status"] in _ARCHIVABLE_STATUSES and project["updated_at"] <= cutoff)
        )
        and (project["assets"] or (project.get("manifest_path") and project["manifest_path"].is_file()))
    ]
    eligible_projects.sort(key=lambda item: (item["updated_at"] or 0.0, str(item.get("task_id") or "")))
    if after_updated_at is not None and after_task_id:
        cursor = (float(after_updated_at), str(after_task_id))
        eligible_projects = [
            project for project in eligible_projects
            if (float(project.get("updated_at") or 0.0), str(project.get("task_id") or "")) > cursor
        ]

    candidates = []
    for scanned_index, project in enumerate(eligible_projects, start=1):
        if progress_callback:
            progress_callback({
                "phase": "inventory",
                "task_id": str(project.get("task_id") or ""),
                "projects_scanned": scanned_index,
                "projects_total": len(eligible_projects),
            })
        if _project_needs_archive(project, digest_cache, progress_callback=progress_callback):
            candidates.append(project)
            if max_candidates is not None and len(candidates) >= max(1, int(max_candidates)):
                break
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


def _authorize_archive_project(task_id: str, user: User) -> None:
    """Limit search links to the project owner, while admins can inspect any archive."""
    if getattr(user, "is_admin", False):
        return
    from app.models import SavedMusic, VideoTask

    db = SessionLocal()
    try:
        if re.fullmatch(r"music-[0-9]{1,12}", task_id):
            row = db.query(SavedMusic).filter(SavedMusic.id == int(task_id.split("-", 1)[1])).first()
        else:
            row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
        if not row or int(getattr(row, "user_id", -1) or -1) != int(user.id):
            raise HTTPException(404, "Projeto não encontrado.")
    finally:
        db.close()


@router.get("/{task}/search")
def search_google_drive_project_archive(
    task: str,
    current_user: User = Depends(get_current_user),
):
    """Find this project's verified Drive copies and issue short-lived download links."""
    try:
        task = task_key(task)
    except ValueError as exc:
        raise HTTPException(400, "ID de projeto inválido.") from exc
    _authorize_archive_project(task, current_user)
    files = []
    for (kind, filename), entry in sorted(_drive_index_for_task(task).items()):
        sha256 = str(entry.get("sha256") or "").strip().lower()
        try:
            from app.services.central_asset_transfer import archive_path
            size = int(entry.get("size") or 0)
            archive_path(kind, filename)
        except (TypeError, ValueError):
            continue
        if not entry.get("drive_file_id") or not re.fullmatch(r"[0-9a-f]{64}", sha256) or size <= 0:
            continue
        token = _DOWNLOAD_STATE.dumps({"task": task, "kind": kind, "filename": filename})
        files.append({
            "kind": kind,
            "filename": filename,
            "size": size,
            "sha256": sha256,
            "download_url": f"/settings/google-drive/download/{token}",
        })
    return {"task_id": task, "files": files}


@router.get("/download/{token}", include_in_schema=False)
def download_google_drive_archive_file(token: str, request: Request):
    """Stream one indexed asset through a short-lived, signed browser download URL."""
    try:
        payload = _DOWNLOAD_STATE.loads(token, max_age=_DOWNLOAD_TOKEN_TTL_SECONDS)
        task = task_key(payload.get("task"))
        kind = str(payload.get("kind") or "")
        filename = str(payload.get("filename") or "")
        entry = _drive_index_for_task(task).get((kind, filename))
        if not entry or not entry.get("drive_file_id"):
            raise HTTPException(404, "Arquivo não encontrado no backup do projeto.")
        from app.services.central_asset_transfer import archive_path
        archive_path(kind, filename)
    except (BadSignature, SignatureExpired, ValueError, TypeError) as exc:
        raise HTTPException(403, "Link de download inválido ou expirado; faça a busca novamente.") from exc
    response = GoogleDriveArchive.for_asset(entry).stream_response(
        entry,
        range_header=request.headers.get("range", ""),
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )
    if hasattr(response, "headers"):
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _remove_fully_archived_aliases(projects: Dict[str, Dict[str, Any]], inodes: Dict[tuple[int, int], Dict[str, Any]]) -> int:
    """Remove only known links after every owning task has a matching Drive copy."""
    from app.config import COVERS_OUTPUT_DIR
    from app.services.production_manifest import _root_dir

    manifest_root = Path(_root_dir()).resolve()
    allowed_roots = [Path(value).resolve() for value in roots().values()]
    allowed_roots.append(Path(COVERS_OUTPUT_DIR).resolve())
    allowed_roots.append(manifest_root)
    index_cache: Dict[str, Dict[tuple[str, str], Dict[str, Any]]] = {}

    def free_bytes_for_managed_storage() -> int:
        paths_by_device = {}
        for root in allowed_roots:
            try:
                if root.exists():
                    paths_by_device.setdefault(int(root.stat().st_dev), root)
            except OSError:
                continue
        total = 0
        for root in paths_by_device.values():
            try:
                total += int(shutil.disk_usage(root).free)
            except OSError:
                continue
        return total

    freed_before = free_bytes_for_managed_storage()
    cutoff = time.time() - 24 * 60 * 60

    for group in inodes.values():
        owners = group["owners"]
        if not owners or len(group["paths"]) != int(group["link_count"] or 0):
            continue
        # Saved Music and Suno outputs are archived for recovery, but remain in
        # the product library by design.
        if any(projects.get(task_id, {}).get("preserve_local") for task_id in owners):
            continue
        if any(
            kind == "script"
            for refs in owners.values()
            for kind, _filename in refs
        ):
            continue
        # Keep videos for successful/finalized projects. Failed project videos
        # are still protected by the checksum check below and remain recoverable.
        if any(
            kind == "video" and preserves_final_video(projects.get(task_id, {}).get("status"))
            for task_id, refs in owners.items()
            for kind, _ in refs
        ):
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
            verified_kinds = {
                indexed_kind
                for (indexed_kind, _indexed_filename), entry in index.items()
                if str(entry.get("sha256") or "") == expected_sha
                and int(entry.get("size") or -1) == expected_size
            }
            if any(kind not in verified_kinds for kind, _filename in refs):
                complete = False
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

    freed_after = free_bytes_for_managed_storage()
    return max(0, int(freed_after - freed_before))


def _queue_confirmed_worker_cleanup(job_id: str, payload: Dict[str, Any]) -> str:
    task_ids = list(dict.fromkeys(
        str(task_id) for task_id in (payload.get("worker_cleanup_task_ids") or [])
        if str(task_id).strip()
    ))
    if not task_ids:
        return "not_needed"
    try:
        from app.redis_client import queue

        worker_job = queue.enqueue(
            cleanup_confirmed_worker_cache_job,
            task_ids,
            max_tasks=len(task_ids),
            job_timeout=4 * 60 * 60,
            result_ttl=7 * 24 * 60 * 60,
            failure_ttl=7 * 24 * 60 * 60,
            description="Remover do CX33 arquivos confirmados no Google Drive",
        )
        if hasattr(worker_job, "id"):
            payload["worker_cleanup_job_id"] = str(worker_job.id)
            return "queued"
        if isinstance(worker_job, dict):
            payload["worker_cleanup_result"] = worker_job
            if not worker_job.get("enabled", True):
                payload["worker_cleanup_message"] = (
                    "O CX33 não está configurado para consultar o arquivo central; nenhum arquivo dele foi removido."
                )
            elif int(worker_job.get("errors") or 0) > 0:
                payload["worker_cleanup_message"] = (
                    "Alguns arquivos do CX33 não puderam ser verificados e permaneceram no disco."
                )
            return "finished"
        return "queued"
    except Exception as exc:
        logger.warning(
            "Google Drive migration could not queue CX33 cleanup: job_id=%s exception_type=%s",
            job_id, type(exc).__name__,
        )
        payload["worker_cleanup_message"] = (
            "O CX33 não recebeu a solicitação; os arquivos locais dele foram mantidos."
        )
        return "not_queued"


def _run_migration(
    job_id: str,
    user_id: int,
    limit: int,
    after_updated_at: Optional[float] = None,
    after_task_id: str = "",
    defer_worker_cleanup: bool = False,
    staged_mode: bool = False,
) -> None:
    payload = _read_job(job_id)
    completed_projects_before = int(
        payload.get("overall_processed_projects") or payload.get("completed_projects_before") or 0
    )
    completed_files_before = int(
        payload.get("overall_processed_files") or payload.get("completed_files_before") or 0
    )
    archived_bytes_before = int(
        payload.get("overall_archived_bytes") or payload.get("archived_bytes_before") or 0
    )
    archived_before = int(payload.get("archived") or 0)
    archived_projects_before = int(payload.get("archived_projects") or 0)
    skipped_before = int(payload.get("skipped") or 0)
    bytes_freed_before = int(payload.get("bytes_freed") or 0)
    error_count_before = int(payload.get("error_count") or 0)
    batch_baseline_projects = int(payload.get("stage_baseline_projects") or completed_projects_before)
    errors = list(payload.get("errors") or [])
    payload.update({
        "status": "processing",
        "started_at": payload.get("started_at") or datetime.now(timezone.utc).isoformat(),
        "overall_processed_projects": completed_projects_before,
        "overall_processed_files": completed_files_before,
        "overall_archived_bytes": archived_bytes_before,
        "batch_size": _MIGRATION_BATCH_SIZE,
        "batch_baseline_projects": batch_baseline_projects,
        "batch_processed_projects": max(0, completed_projects_before - batch_baseline_projects),
        "batch_project_total": min(
            _MIGRATION_BATCH_SIZE,
            max(0, int(payload.get("overall_total_projects") or 0) - batch_baseline_projects),
        ) or _MIGRATION_BATCH_SIZE,
        "batch_number": math.ceil(batch_baseline_projects / _MIGRATION_BATCH_SIZE) + 1,
        "batch_total": int(math.ceil(int(payload.get("overall_total_projects") or 0) / _MIGRATION_BATCH_SIZE)),
        "batch_duration_target_seconds": _MIGRATION_BATCH_REFERENCE_SECONDS,
    })
    _write_job(job_id, payload)
    archived = 0
    skipped = 0
    bytes_freed = 0
    archived_projects = 0
    processed_projects = 0
    processed_files = 0
    archived_bytes = 0
    batch_error_count = 0
    selected_task_ids = []
    try:
        inventory_last_write = 0.0

        def report_inventory(progress: Dict[str, Any]) -> None:
            nonlocal inventory_last_write
            now = time.monotonic()
            phase = str(progress.get("phase") or "inventory")
            verified_bytes = int(progress.get("asset_verified_bytes") or 0)
            asset_size = int(progress.get("asset_size_bytes") or 0)
            should_write = (
                phase != payload.get("stage_phase")
                or now - inventory_last_write >= 3
                or (asset_size > 0 and verified_bytes >= asset_size)
            )
            if not should_write:
                return
            inventory_last_write = now
            payload["stage_phase"] = phase
            if progress.get("manifests_scanned") is not None:
                payload["inventory_manifests_scanned"] = int(progress["manifests_scanned"])
            if progress.get("projects_scanned") is not None:
                payload["inventory_projects_scanned"] = int(progress["projects_scanned"])
                payload["inventory_projects_total"] = int(progress.get("projects_total") or 0)
            if not payload.get("overall_total_projects") and progress.get("projects_total"):
                payload["overall_total_projects"] = int(progress["projects_total"])
                total_projects = int(payload["overall_total_projects"])
                payload["batch_total"] = int(math.ceil(total_projects / _MIGRATION_BATCH_SIZE))
                payload["batch_project_total"] = min(
                    _MIGRATION_BATCH_SIZE,
                    max(0, total_projects - batch_baseline_projects),
                ) or _MIGRATION_BATCH_SIZE
            if progress.get("task_id"):
                payload["current_project_task_id"] = str(progress["task_id"])
            if "asset" in progress:
                payload["current_asset"] = str(progress.get("asset") or "")
                payload["current_asset_size_bytes"] = asset_size
                payload["current_asset_verification_bytes"] = verified_bytes
            _write_job(job_id, payload)

        if staged_mode:
            payload.update({
                "stage_phase": "inventory",
                "current_project_task_id": "",
                "current_asset": "",
                "current_asset_size_bytes": 0,
                "current_asset_verification_bytes": 0,
                "inventory_manifests_scanned": 0,
                "inventory_projects_scanned": 0,
            })
            _write_job(job_id, payload)
            projects, inodes, candidates = _project_archive_inventory(
                after_updated_at=after_updated_at,
                after_task_id=after_task_id,
                max_candidates=limit,
                progress_callback=report_inventory,
            )
        else:
            projects, inodes, candidates = _project_archive_inventory()
            if after_updated_at is not None and after_task_id:
                cursor = (after_updated_at, after_task_id)
                candidates = [
                    project for project in candidates
                    if (float(project.get("updated_at") or 0.0), str(project.get("task_id") or "")) > cursor
                ]
        selected = candidates[:limit]
        selected_task_ids = [
            str(project["task_id"]) for project in selected
            if not project.get("preserve_local")
        ]
        payload["total_candidates"] = len(selected)
        if staged_mode:
            known_total = int(payload.get("overall_total_projects") or 0)
            remaining = max(0, known_total - completed_projects_before - len(selected)) if known_total else 0
            payload["remaining_candidates"] = remaining
            payload["has_more_candidates"] = bool(selected and remaining)
            if selected and known_total:
                payload["batch_number"] = math.ceil(batch_baseline_projects / _MIGRATION_BATCH_SIZE) + 1
                payload["batch_project_total"] = len(selected)
                payload["batch_processed_projects"] = max(
                    0, completed_projects_before - batch_baseline_projects
                )
                payload["batch_project_current_index"] = 1
                payload["batch_total"] = int(math.ceil(known_total / _MIGRATION_BATCH_SIZE))
            elif not selected and known_total:
                # The estimate can become stale while the job waits in the queue.
                # If no projects remain after checking the cursor, close the bar
                # at the number actually confirmed instead of leaving it at 99%.
                payload["overall_total_projects"] = completed_projects_before
                payload["overall_total_files"] = completed_files_before
                payload["overall_total_bytes"] = archived_bytes_before
                payload["remaining_candidates"] = 0
                payload["has_more_candidates"] = False
                payload["batch_total"] = int(math.ceil(completed_projects_before / _MIGRATION_BATCH_SIZE))
                payload["batch_project_total"] = 0
                payload["batch_processed_projects"] = 0
                payload["batch_project_current_index"] = 0
        else:
            payload["remaining_candidates"] = max(0, len(candidates) - len(selected))
            payload["has_more_candidates"] = len(candidates) > len(selected)
        payload["batch_total_files"] = sum(
            len(_migration_assets_for_project(project)) for project in selected
        )
        payload["batch_total_bytes"] = sum(
            _migration_asset_size(asset)
            for project in selected
            for asset in _migration_assets_for_project(project).values()
        )
        if selected:
            payload["last_candidate_cursor"] = {
                "updated_at": float(selected[-1].get("updated_at") or 0.0),
                "task_id": str(selected[-1].get("task_id") or ""),
            }
            payload["current_project_index"] = completed_projects_before + 1
            payload["current_project_total"] = int(payload.get("overall_total_projects") or len(selected))
            payload["stage_phase"] = "uploading"
            payload["current_asset"] = ""
            payload["current_asset_size_bytes"] = 0
            payload["current_asset_verification_bytes"] = 0
        _write_job(job_id, payload)
        for project_index, project in enumerate(selected, start=1):
            task_id = project["task_id"]
            archive = GoogleDriveArchive.for_task(task_id, user_id=user_id)
            account_id = archive._settings().get("account_id")
            if account_id is not None:
                pin_task_drive_account(task_id, account_id)
            # Create the project's Drive folder before parallel uploads so
            # worker threads cannot race to create duplicate folders.
            archive.prepare_task_folder(task_id)
            assets = _migration_assets_for_project(project)
            project_ok = bool(assets)
            staged_metadata = []
            try:
                for asset in assets.values():
                    if "content" not in asset:
                        continue
                    temp_dir = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
                    with tempfile.NamedTemporaryFile(
                        mode="wb", prefix="codexia-drive-", suffix=".json",
                        dir=temp_dir, delete=False,
                    ) as staged:
                        staged.write(str(asset.pop("content") or "").encode("utf-8"))
                        asset["path"] = Path(staged.name)
                        staged_metadata.append(Path(staged.name))
                asset_items = list(assets.values())
                total_projects = int(payload.get("overall_total_projects") or 0) or len(selected)
                payload.update({
                    "current_project_index": completed_projects_before + project_index,
                    "current_project_total": max(total_projects, completed_projects_before + len(selected)),
                    "batch_project_current_index": project_index,
                    "current_asset_index": 0,
                    "current_asset_total": len(asset_items),
                    "current_asset": "",
                    "current_asset_size_bytes": 0,
                    "current_asset_verification_bytes": 0,
                    "current_asset_uploaded_bytes": 0,
                    "current_project_task_id": str(task_id),
                    "stage_phase": "uploading",
                })
                _write_job(job_id, payload)
                max_parallel_uploads = max(1, min(3, len(asset_items)))
                upload_lock = threading.Lock()
                progress_state = {}
                last_progress_write = 0.0

                for asset_index, asset in enumerate(asset_items, start=1):
                    path = Path(asset["path"])
                    try:
                        stat = path.stat()
                        source_size = int(stat.st_size)
                    except OSError:
                        stat = None
                        source_size = 0
                    progress_state[asset_index] = {
                        "filename": str(asset.get("filename") or ""),
                        "size": source_size,
                        "verified": 0,
                        "uploaded": 0,
                        "phase": "queued",
                    }

                def publish_parallel_progress(force: bool = False) -> None:
                    nonlocal last_progress_write
                    with upload_lock:
                        now = time.monotonic()
                        if not force and now - last_progress_write < 2:
                            return
                        last_progress_write = now
                        entries = list(progress_state.values())
                        pending_entries = [
                            item for item in entries
                            if item.get("phase") in {"queued", "verifying", "uploading"}
                        ]
                        total_size = sum(max(0, int(item.get("size") or 0)) for item in pending_entries)
                        verified = sum(max(0, int(item.get("verified") or 0)) for item in pending_entries)
                        uploaded = sum(max(0, int(item.get("uploaded") or 0)) for item in pending_entries)
                        active = sum(item.get("phase") in {"verifying", "uploading"} for item in entries)
                        payload.update({
                            "current_project_index": completed_projects_before + project_index,
                            "current_project_total": max(total_projects, completed_projects_before + len(selected)),
                            "current_asset_index": min(len(asset_items), processed_files + 1),
                            "current_asset_total": len(asset_items),
                            "current_asset": (f"Envio paralelo: {active} ativo(s), limite {max_parallel_uploads}" if active else "Preparando próximo arquivo"),
                            "current_asset_size_bytes": total_size,
                            "current_asset_verification_bytes": verified,
                            "current_asset_uploaded_bytes": uploaded,
                            "current_asset_percent": min(100, int(uploaded * 100 / total_size)) if total_size else 0,
                            "parallel_uploads_active": active,
                            "parallel_uploads_limit": max_parallel_uploads,
                            "stage_phase": "verifying_asset" if any(
                                item.get("phase") == "verifying" for item in entries
                            ) else "uploading",
                        })
                        _write_job(job_id, payload)

                def upload_one(asset_index: int, asset: Dict[str, Any]):
                    path = Path(asset["path"])
                    try:
                        source_stat = path.stat()
                        source_size = int(source_stat.st_size)
                    except OSError as exc:
                        return asset_index, asset, 0, None, exc

                    def report_verification_progress(processed: int, total: int) -> None:
                        with upload_lock:
                            state = progress_state[asset_index]
                            state["phase"] = "verifying"
                            state["size"] = int(total or source_size)
                            state["verified"] = int(processed)
                        publish_parallel_progress()

                    def report_upload_progress(uploaded: int, total: int) -> None:
                        with upload_lock:
                            state = progress_state[asset_index]
                            state["phase"] = "uploading"
                            state["size"] = int(total or source_size)
                            state["uploaded"] = int(uploaded)
                            state["verified"] = 0
                        publish_parallel_progress()

                    with upload_lock:
                        progress_state[asset_index]["phase"] = "verifying"
                    publish_parallel_progress(force=True)
                    try:
                        try:
                            entry = archive.upload_path(
                                task_id, asset["kind"], asset["filename"], path,
                                progress_callback=report_upload_progress,
                                verification_callback=report_verification_progress,
                            )
                        except DriveAssetConflict as conflict:
                            source_sha = str(conflict.source_sha256 or "").strip().lower()
                            if not re.fullmatch(r"[0-9a-f]{64}", source_sha):
                                source_sha = digest(path)
                            versioned_name = _versioned_archive_filename(asset["filename"], source_sha)
                            entry = archive.upload_path(
                                task_id, asset["kind"], versioned_name, path,
                                progress_callback=report_upload_progress,
                                verification_callback=report_verification_progress,
                            )
                        final_stat = path.stat()
                        if (int(entry.get("size") or -1) != source_size
                                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256") or ""))):
                            raise RuntimeError("O Drive não confirmou o tamanho e checksum do arquivo.")
                        if (int(final_stat.st_size) != int(source_stat.st_size)
                                or int(final_stat.st_mtime_ns) != int(source_stat.st_mtime_ns)
                                or int(final_stat.st_ctime_ns) != int(source_stat.st_ctime_ns)):
                            raise RuntimeError("O arquivo local mudou durante o envio e foi preservado para nova conferência.")
                        return asset_index, asset, source_size, entry, None
                    except Exception as exc:
                        return asset_index, asset, source_size, None, exc

                publish_parallel_progress(force=True)
                with ThreadPoolExecutor(max_workers=max_parallel_uploads) as upload_pool:
                    futures = [
                        upload_pool.submit(upload_one, asset_index, asset)
                        for asset_index, asset in enumerate(asset_items, start=1)
                    ]
                    for future in as_completed(futures):
                        asset_index, asset, source_size, entry, upload_error = future.result()
                        try:
                            if upload_error is not None:
                                raise upload_error
                            register_asset(task_id, entry)
                            archived += 1
                            archived_bytes += source_size
                            with upload_lock:
                                progress_state[asset_index]["phase"] = "completed"
                                progress_state[asset_index]["uploaded"] = source_size
                                progress_state[asset_index]["verified"] = source_size
                        except Exception as exc:
                            project_ok = False
                            errors.append(f"{task_id}/{asset['filename']}: {str(exc)[:180]}")
                            batch_error_count += 1
                            with upload_lock:
                                progress_state[asset_index]["phase"] = "failed"
                                progress_state[asset_index]["uploaded"] = 0
                        finally:
                            processed_files += 1
                            with upload_lock:
                                payload.update({
                                    "archived": archived_before + archived,
                                    "archived_projects": archived_projects_before + archived_projects,
                                    "processed_files": completed_files_before + processed_files,
                                    "archived_bytes": archived_bytes_before + archived_bytes,
                                    "overall_processed_files": completed_files_before + processed_files,
                                    "overall_archived_bytes": archived_bytes_before + archived_bytes,
                                    "skipped": skipped_before + skipped,
                                    "bytes_freed": bytes_freed_before + bytes_freed,
                                    "error_count": error_count_before + batch_error_count,
                                    "errors": errors[-50:],
                                })
                                # A final update is useful even when the normal
                                # progress-write throttle has not elapsed.
                                last_progress_write = 0.0
                            publish_parallel_progress(force=True)
                if project_ok:
                    archived_projects += 1
            except Exception as exc:
                errors.append(f"{task_id}: {str(exc)[:200]}")
                batch_error_count += 1
                project_ok = False
            finally:
                for path in staged_metadata:
                    path.unlink(missing_ok=True)
            processed_projects += 1
            payload.update({
                "archived": archived_before + archived,
                "archived_projects": archived_projects_before + archived_projects,
                "processed_projects": completed_projects_before + processed_projects,
                "overall_processed_projects": completed_projects_before + processed_projects,
                "batch_processed_projects": processed_projects,
                "batch_project_current_index": 0,
                "batch_project_total": len(selected),
                "overall_processed_files": completed_files_before + processed_files,
                "overall_archived_bytes": archived_bytes_before + archived_bytes,
                "archived_bytes": archived_bytes_before + archived_bytes,
                "processed_files": completed_files_before + processed_files,
                "skipped": skipped_before + skipped,
                "bytes_freed": bytes_freed_before + bytes_freed,
                "error_count": error_count_before + batch_error_count,
                "errors": errors[-50:],
            })
            _write_job(job_id, payload)
        # This sweep sees shared hard links across projects. It removes them only
        # after each owning project has a checksum-verified Drive entry.
        payload["stage_phase"] = "cleaning_main"
        _write_job(job_id, payload)
        cleanup_inodes = inodes
        if staged_mode:
            selected_owners = set(selected_task_ids)
            cleanup_inodes = {
                inode_key: group
                for inode_key, group in inodes.items()
                if selected_owners.intersection(group.get("owners") or {})
            }
        bytes_freed = _remove_fully_archived_aliases(projects, cleanup_inodes)
        payload["current_stage_main_cleanup_complete"] = True
        payload["main_cleanup_bytes_freed_this_stage"] = int(bytes_freed)
        payload["stage_phase"] = "cleaning_worker"
        _write_job(job_id, payload)
    except Exception as exc:
        errors.append(str(exc)[:240])
        batch_error_count += 1

    if defer_worker_cleanup:
        previous_ids = list(payload.get("worker_cleanup_task_ids") or [])
        payload["worker_cleanup_task_ids"] = list(dict.fromkeys(previous_ids + selected_task_ids))
        worker_cleanup_status = "deferred" if payload["worker_cleanup_task_ids"] else "not_needed"
    else:
        cleanup_payload = {**payload, "worker_cleanup_task_ids": selected_task_ids}
        worker_cleanup_status = _queue_confirmed_worker_cleanup(job_id, cleanup_payload)
        payload.update({
            key: value for key, value in cleanup_payload.items()
            if key in {"worker_cleanup_job_id", "worker_cleanup_result", "worker_cleanup_message"}
        })

    payload["worker_cleanup_status"] = worker_cleanup_status
    current_error_count = error_count_before + batch_error_count
    has_more = bool(payload.get("has_more_candidates"))
    final_status = "completed" if current_error_count == 0 else (
        "completed_with_errors" if archived_before + archived or skipped_before + skipped else "failed"
    )
    payload.update({
        "status": "processing" if defer_worker_cleanup else final_status,
        "archived": archived_before + archived,
        "archived_projects": archived_projects_before + archived_projects,
        "processed_projects": completed_projects_before + processed_projects,
        "processed_files": completed_files_before + processed_files,
        "archived_bytes": archived_bytes_before + archived_bytes,
        "overall_processed_projects": completed_projects_before + processed_projects,
        "overall_processed_files": completed_files_before + processed_files,
        "overall_archived_bytes": archived_bytes_before + archived_bytes,
        "skipped": skipped_before + skipped,
        "bytes_freed": bytes_freed_before + bytes_freed,
        "error_count": current_error_count,
        "errors": errors[-50:],
    })
    if payload["status"] == "processing":
        payload.pop("finished_at", None)
    else:
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_job(job_id, payload)
    from app.services.server_resource_monitor import invalidate_drive_usage_cache

    invalidate_drive_usage_cache(user_id)


def _run_full_migration(
    job_id: str,
    user_id: int,
    limit: int,
    after_updated_at: Optional[float] = None,
    after_task_id: str = "",
) -> None:
    cursor_updated_at = after_updated_at
    cursor_task_id = after_task_id
    previous_cursors = set()
    try:
        for batch_number in range(1, 501):
            payload = _read_job(job_id)
            payload.update({
                "status": "processing",
                "batch_number": batch_number,
                "current_project_index": int(payload.get("overall_processed_projects") or 0),
                "current_asset_index": 0,
                "current_asset_total": 0,
                "current_asset": "",
            })
            _write_job(job_id, payload)
            _run_migration(
                job_id, user_id, limit, cursor_updated_at, cursor_task_id,
                defer_worker_cleanup=True,
            )
            payload = _read_job(job_id)
            if not payload.get("has_more_candidates"):
                break
            next_cursor = payload.get("last_candidate_cursor") or {}
            next_updated_at = next_cursor.get("updated_at")
            next_task_id = str(next_cursor.get("task_id") or "")
            cursor_key = (float(next_updated_at or 0.0), next_task_id)
            if next_updated_at is None or not next_task_id or cursor_key in previous_cursors:
                errors = list(payload.get("errors") or [])
                errors.append("O backup não avançou para o próximo lote; os itens não confirmados foram mantidos.")
                payload.update({
                    "status": "completed_with_errors" if int(payload.get("archived") or 0) else "failed",
                    "error_count": int(payload.get("error_count") or 0) + 1,
                    "errors": errors[-50:],
                    "has_more_candidates": False,
                    "remaining_candidates": 0,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                _write_job(job_id, payload)
                break
            previous_cursors.add(cursor_key)
            cursor_updated_at = float(next_updated_at)
            cursor_task_id = next_task_id
        else:
            payload = _read_job(job_id)
            errors = list(payload.get("errors") or [])
            errors.append("O limite de 500 lotes foi atingido; inicie outro backup para continuar.")
            payload.update({
                "status": "completed_with_errors" if int(payload.get("archived") or 0) else "failed",
                "error_count": int(payload.get("error_count") or 0) + 1,
                "errors": errors[-50:],
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            _write_job(job_id, payload)
    except Exception as exc:
        payload = _read_job(job_id)
        errors = list(payload.get("errors") or [])
        errors.append(str(exc)[:240])
        payload.update({
            "status": "completed_with_errors" if int(payload.get("archived") or 0) else "failed",
            "error_count": int(payload.get("error_count") or 0) + 1,
            "errors": errors[-50:],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_job(job_id, payload)

    payload = _read_job(job_id)
    payload["worker_cleanup_status"] = _queue_confirmed_worker_cleanup(job_id, payload)
    if payload.get("status") == "processing":
        error_count = int(payload.get("error_count") or 0)
        payload["status"] = "completed" if error_count == 0 else (
            "completed_with_errors" if int(payload.get("archived") or 0) else "failed"
        )
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_job(job_id, payload)
    from app.services.server_resource_monitor import invalidate_drive_usage_cache

    invalidate_drive_usage_cache(user_id)


def _queue_migration_stage(
    job_id: str,
    user_id: int,
    after_updated_at: Optional[float] = None,
    after_task_id: str = "",
    *,
    resume_current_stage: bool = False,
) -> Optional[str]:
    """Queue one project-sized migration stage on the persistent RQ worker."""
    payload = _read_job(job_id)
    payload.update({
        "status": "queued",
        "stage_phase": "queued",
        "stage_cursor_after_updated_at": after_updated_at,
        "stage_cursor_after_task_id": str(after_task_id or ""),
        "batch_size": _MIGRATION_BATCH_SIZE,
        "batch_duration_target_seconds": _MIGRATION_BATCH_REFERENCE_SECONDS,
    })
    if not resume_current_stage:
        batch_baseline = int(payload.get("overall_processed_projects") or 0)
        total_projects = int(payload.get("overall_total_projects") or 0)
        payload.update({
            "stage_baseline_projects": batch_baseline,
            "batch_baseline_projects": batch_baseline,
            "batch_processed_projects": 0,
            "batch_project_total": min(
                _MIGRATION_BATCH_SIZE,
                max(0, total_projects - batch_baseline),
            ) or _MIGRATION_BATCH_SIZE,
            "batch_project_current_index": 0,
            "batch_number": math.ceil(batch_baseline / _MIGRATION_BATCH_SIZE) + 1,
            "batch_total": int(math.ceil(total_projects / _MIGRATION_BATCH_SIZE)) if total_projects else 0,
            "batch_started_at": "",
            "batch_elapsed_seconds": 0,
            "stage_baseline_files": int(payload.get("overall_processed_files") or 0),
            "stage_baseline_archived_bytes": int(payload.get("overall_archived_bytes") or 0),
            "stage_baseline_archived": int(payload.get("archived") or 0),
            "stage_baseline_archived_projects": int(payload.get("archived_projects") or 0),
            "stage_baseline_bytes_freed": int(payload.get("bytes_freed") or 0),
            "stage_baseline_main_bytes_freed": int(payload.get("main_bytes_freed") or 0),
            "stage_baseline_worker_bytes_freed": int(payload.get("worker_bytes_freed") or 0),
            "stage_baseline_error_count": int(payload.get("error_count") or 0),
            "stage_baseline_errors": list(payload.get("errors") or [])[-50:],
            "current_stage_main_cleanup_complete": False,
            "main_cleanup_bytes_freed_this_stage": 0,
            "current_asset_uploaded_bytes": 0,
            "current_asset_percent": 0,
            "current_project_task_id": "",
            "current_asset": "",
            "current_asset_size_bytes": 0,
            "current_asset_total": 0,
            "current_asset_index": 0,
            "worker_cleanup_status": "pending",
            "worker_cleanup_result": None,
            "worker_cleanup_message": "",
        })
    else:
        payload["stage_phase"] = "cleaning_worker"
    _write_job(job_id, payload)
    from app.redis_client import queue

    worker_job = queue.enqueue(
        _run_migration_stage,
        job_id,
        int(user_id),
        after_updated_at,
        str(after_task_id or ""),
        job_timeout=4 * 60 * 60,
        result_ttl=7 * 24 * 60 * 60,
        failure_ttl=7 * 24 * 60 * 60,
        description=f"Arquivar lote de até 10 projetos no Google Drive ({job_id[:8]})",
    )
    worker_job_id = str(getattr(worker_job, "id", "") or "")
    if worker_job_id:
        payload = _read_job(job_id)
        payload["stage_job_id"] = worker_job_id
        payload["stage_job_status"] = "queued"
        _write_job(job_id, payload)
    return worker_job_id or None


def _run_migration_stage(
    job_id: str,
    user_id: int,
    after_updated_at: Optional[float] = None,
    after_task_id: str = "",
) -> None:
    """Archive, verify, and clean up to ten projects, then enqueue the next batch."""
    payload = _read_job(job_id)
    batch_baseline = int(payload.get("stage_baseline_projects") or 0)
    total_projects = int(payload.get("overall_total_projects") or 0)
    if not payload.get("current_stage_main_cleanup_complete"):
        payload["batch_started_at"] = datetime.now(timezone.utc).isoformat()
    payload.update({
        "status": "processing",
        "stage_job_status": "started",
        "stage_phase": "cleaning_worker" if payload.get("current_stage_main_cleanup_complete") else "inventory",
        "started_at": payload.get("started_at") or datetime.now(timezone.utc).isoformat(),
        "stage_cursor_after_updated_at": after_updated_at,
        "stage_cursor_after_task_id": str(after_task_id or ""),
        "batch_size": _MIGRATION_BATCH_SIZE,
        "batch_baseline_projects": batch_baseline,
        "batch_processed_projects": max(
            0, int(payload.get("overall_processed_projects") or 0) - batch_baseline
        ),
        "batch_project_total": min(
            _MIGRATION_BATCH_SIZE,
            max(0, total_projects - batch_baseline),
        ) or _MIGRATION_BATCH_SIZE,
        "batch_number": math.ceil(batch_baseline / _MIGRATION_BATCH_SIZE) + 1,
        "batch_total": int(math.ceil(total_projects / _MIGRATION_BATCH_SIZE)) if total_projects else 0,
        "batch_duration_target_seconds": _MIGRATION_BATCH_REFERENCE_SECONDS,
    })
    _write_job(job_id, payload)

    try:
        if not payload.get("current_stage_main_cleanup_complete"):
            _run_migration(
                job_id,
                int(user_id),
                _MIGRATION_BATCH_SIZE,
                after_updated_at,
                str(after_task_id or ""),
                defer_worker_cleanup=True,
                staged_mode=True,
            )

        payload = _read_job(job_id)
        if (not payload.get("current_stage_main_cleanup_complete")
                and bool(payload.get("has_more_candidates"))
                and int(payload.get("overall_processed_projects") or 0)
                <= int(payload.get("stage_baseline_projects") or 0)
                and not payload.get("current_project_task_id")):
            raise RuntimeError("O inventário não concluiu esta etapa; ela pode ser retomada com segurança.")
        task_ids = list(dict.fromkeys(
            str(value) for value in (payload.get("worker_cleanup_task_ids") or [])
            if str(value).strip()
        ))
        stage_baseline_worker = int(payload.get("stage_baseline_worker_bytes_freed") or 0)
        stage_baseline_main = int(payload.get("stage_baseline_main_bytes_freed") or 0)
        stage_baseline_bytes = int(payload.get("stage_baseline_bytes_freed") or 0)
        stage_main_delta = max(0, int(payload.get("main_cleanup_bytes_freed_this_stage") or 0))
        payload["stage_phase"] = "cleaning_worker"
        payload["worker_cleanup_status"] = "processing" if task_ids else "not_needed"
        _write_job(job_id, payload)

        cleanup_result = None
        if task_ids:
            cleanup_result = cleanup_confirmed_worker_cache_job(task_ids, max_tasks=len(task_ids))
        worker_delta = int((cleanup_result or {}).get("reclaimed_bytes") or 0)
        payload = _read_job(job_id)
        if cleanup_result is not None:
            payload["worker_cleanup_result"] = cleanup_result
            payload["worker_cleanup_status"] = "finished"
            if not cleanup_result.get("enabled"):
                payload["worker_cleanup_message"] = (
                    "O CX33 não confirmou a limpeza; os arquivos locais dele foram mantidos."
                )
            elif int(cleanup_result.get("errors") or 0) > 0:
                payload["worker_cleanup_message"] = (
                    "Alguns arquivos do CX33 não puderam ser verificados e permaneceram no disco."
                )
            if (not cleanup_result.get("enabled") or int(cleanup_result.get("errors") or 0) > 0):
                errors = list(payload.get("errors") or [])
                errors.append(str(payload.get("worker_cleanup_message") or "A limpeza do CX33 ficou incompleta."))
                payload["errors"] = errors[-50:]
                payload["error_count"] = int(payload.get("error_count") or 0) + 1
        else:
            payload["worker_cleanup_status"] = "not_needed"
        payload["worker_cleanup_task_ids"] = []
        payload["main_bytes_freed"] = stage_baseline_main + stage_main_delta
        payload["worker_bytes_freed"] = stage_baseline_worker + worker_delta
        payload["bytes_freed"] = stage_baseline_bytes + stage_main_delta + worker_delta
        payload["current_stage_main_cleanup_complete"] = False
        payload["stage_job_status"] = "finished"
        payload["stage_phase"] = "stage_finished"

        batch_processed = max(
            0,
            min(
                _MIGRATION_BATCH_SIZE,
                int(payload.get("overall_processed_projects") or 0) - batch_baseline,
            ),
        )
        batch_started_at = _timestamp(payload.get("batch_started_at"), 0.0)
        if batch_processed and batch_started_at:
            marker_cursor = payload.get("last_candidate_cursor") or {}
            batch_marker = f"{batch_baseline}:{marker_cursor.get('updated_at')}:{marker_cursor.get('task_id') or ''}"
            recorded_marker = str(payload.get("last_batch_duration_marker") or "")
            if batch_marker != recorded_marker:
                history = list(payload.get("batch_duration_history") or [])
                history.append({
                    "seconds": max(1, int(time.time() - batch_started_at)),
                    "projects": batch_processed,
                })
                payload["batch_duration_history"] = history[-10:]
                payload["last_batch_duration_marker"] = batch_marker

        has_more = bool(payload.get("has_more_candidates"))
        pause_requested = bool(payload.get("pause_requested"))
        cursor = payload.get("last_candidate_cursor") or {}
        cursor_updated_at = cursor.get("updated_at")
        cursor_task_id = str(cursor.get("task_id") or "")
        if has_more and (cursor_updated_at is None or not cursor_task_id):
            errors = list(payload.get("errors") or [])
            errors.append("Não foi possível identificar o próximo projeto; os arquivos sem confirmação foram mantidos.")
            payload["errors"] = errors[-50:]
            payload["error_count"] = int(payload.get("error_count") or 0) + 1
            payload["has_more_candidates"] = False
            has_more = False

        if has_more and pause_requested:
            payload.update({
                "status": "paused",
                "stage_phase": "paused",
                "pause_requested": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            _write_job(job_id, payload)
            return

        if has_more:
            _write_job(job_id, payload)
            _queue_migration_stage(job_id, int(user_id), float(cursor_updated_at), cursor_task_id)
            return

        error_count = int(payload.get("error_count") or 0)
        if error_count:
            final_status = "completed_with_errors" if int(payload.get("archived") or 0) else "failed"
        else:
            final_status = "completed"
        payload.update({
            "status": final_status,
            "stage_phase": "completed",
            "pause_requested": False,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "remaining_candidates": 0,
            "has_more_candidates": False,
        })
        _write_job(job_id, payload)
        from app.services.server_resource_monitor import invalidate_drive_usage_cache

        invalidate_drive_usage_cache(int(user_id))
    except Exception as exc:
        payload = _read_job(job_id)
        errors = list(payload.get("errors") or [])
        errors.append(f"Etapa interrompida: {str(exc)[:220]}")
        payload.update({
            "status": "interrupted",
            "stage_job_status": "failed",
            "stage_phase": "interrupted",
            "resume_available": True,
            "errors": errors[-50:],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_job(job_id, payload)


@router.get("/migrate/estimate")
def estimate_google_drive_migration(current_user: User = Depends(get_current_admin_user)):
    """Read-only estimate of stable project assets eligible for the Drive archive."""
    archive = GoogleDriveArchive(user_id=current_user.id)
    try:
        if not archive.is_configured():
            raise HTTPException(400, "Conecte o Google Drive antes de estimar o backup.")
    except GoogleDriveArchiveError as exc:
        raise HTTPException(503, str(exc)) from exc

    _projects, _inodes, candidates = _project_archive_inventory()
    total_files = 0
    total_bytes = 0
    for project in candidates:
        assets = _migration_assets_for_project(project)
        total_files += len(assets)
        total_bytes += sum(_migration_asset_size(asset) for asset in assets.values())

    return {
        "status": "estimated",
        "total_projects": len(candidates),
        "total_files": total_files,
        "total_bytes": total_bytes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/migrate/start")
def start_google_drive_migration(
    limit: int = _MIGRATION_BATCH_SIZE,
    after_updated_at: Optional[float] = None,
    after_task_id: str = "",
    overall_total_projects: int = 0,
    overall_total_files: int = 0,
    overall_total_bytes: int = 0,
    completed_projects_before: int = 0,
    completed_files_before: int = 0,
    archived_bytes_before: int = 0,
    batch_number: int = 1,
    current_user: User = Depends(get_current_admin_user),
):
    archive = GoogleDriveArchive(user_id=current_user.id)
    try:
        configured = archive.is_configured()
    except GoogleDriveArchiveError as exc:
        raise HTTPException(503, str(exc))
    if not configured:
        raise HTTPException(400, "Conecte o Google Drive antes de iniciar a migração.")
    after_task_id = str(after_task_id or "").strip()
    if (after_updated_at is None) != (not after_task_id):
        raise HTTPException(400, "Cursor do backup incompleto.")
    if after_updated_at is not None and (not math.isfinite(after_updated_at) or after_updated_at < 0):
        raise HTTPException(400, "Cursor do backup inválido.")
    if after_task_id:
        try:
            task_key(after_task_id)
        except ValueError as exc:
            raise HTTPException(400, "Cursor do backup inválido.") from exc
    limit = _MIGRATION_BATCH_SIZE
    job_id = uuid.uuid4().hex
    progress = {
        "overall_total_projects": max(0, int(overall_total_projects)),
        "overall_total_files": max(0, int(overall_total_files)),
        "overall_total_bytes": max(0, int(overall_total_bytes)),
        "completed_projects_before": max(0, int(completed_projects_before)),
        "completed_files_before": max(0, int(completed_files_before)),
        "archived_bytes_before": max(0, int(archived_bytes_before)),
        "batch_number": max(1, int(batch_number)),
        "batch_size": _MIGRATION_BATCH_SIZE,
        "batch_project_total": min(_MIGRATION_BATCH_SIZE, max(0, int(overall_total_projects))),
        "batch_processed_projects": 0,
        "batch_project_current_index": 0,
        "batch_total": int(math.ceil(max(0, int(overall_total_projects)) / _MIGRATION_BATCH_SIZE)),
        "batch_duration_target_seconds": _MIGRATION_BATCH_REFERENCE_SECONDS,
        "batch_duration_history": [],
    }
    _write_job(job_id, {
        "job_id": job_id, "created_by": int(current_user.id), "status": "queued", "requested_limit": limit,
        "archived": 0, "archived_projects": 0, "skipped": 0, "bytes_freed": 0, "errors": [],
        "main_bytes_freed": 0, "worker_bytes_freed": 0,
        "worker_cleanup_status": "not_started", "stage_phase": "queued",
        "started_at": datetime.now(timezone.utc).isoformat(),
        **progress,
        "overall_processed_projects": progress["completed_projects_before"],
        "overall_processed_files": progress["completed_files_before"],
        "overall_archived_bytes": progress["archived_bytes_before"],
    })
    try:
        stage_job_id = _queue_migration_stage(
            job_id, int(current_user.id), after_updated_at, after_task_id,
        )
    except Exception as exc:
        payload = _read_job(job_id)
        payload.update({
            "status": "failed",
            "stage_phase": "queue_failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "errors": [f"Não foi possível enviar a etapa para o worker: {type(exc).__name__}"],
        })
        _write_job(job_id, payload)
        raise HTTPException(503, "A fila do worker está indisponível. Nenhum arquivo foi removido.") from exc
    payload = _read_job(job_id)
    return {
        **payload,
        "stage_job_id": stage_job_id,
        "message": "Backup iniciado em lotes sequenciais de até 10 projetos. Cada arquivo é confirmado no Drive; a limpeza segura só remove cópias verificadas.",
    }


@router.post("/migrate/{job_id}/pause")
def pause_google_drive_migration(job_id: str, current_user: User = Depends(get_current_admin_user)):
    payload = _read_job(job_id)
    if int(payload.get("created_by") or current_user.id) != int(current_user.id):
        raise HTTPException(404, "Migração não encontrada.")
    if str(payload.get("status") or "").lower() not in {"queued", "processing"}:
        raise HTTPException(409, "Só é possível pausar um backup em andamento.")
    payload["pause_requested"] = True
    payload["pause_requested_at"] = datetime.now(timezone.utc).isoformat()
    _write_job(job_id, payload)
    return {
        "job_id": job_id,
        "status": payload.get("status"),
        "message": "A pausa será aplicada após o lote atual e a limpeza segura dos arquivos confirmados.",
    }


@router.post("/migrate/{job_id}/resume")
def resume_google_drive_migration(job_id: str, current_user: User = Depends(get_current_admin_user)):
    payload = _read_job(job_id)
    if int(payload.get("created_by") or current_user.id) != int(current_user.id):
        raise HTTPException(404, "Migração não encontrada.")
    status = str(payload.get("status") or "").lower()
    if status not in {"paused", "interrupted"}:
        raise HTTPException(409, "O backup não está pausado nem interrompido.")
    if status == "interrupted" and not payload.get("stage_job_id"):
        raise HTTPException(409, "Este backup antigo não tem uma etapa retomável. Inicie um novo backup para enviar apenas as pendências.")

    if status == "paused":
        cursor = payload.get("last_candidate_cursor") or {}
        after_updated_at = cursor.get("updated_at")
        after_task_id = str(cursor.get("task_id") or "")
        resume_current_stage = False
    else:
        after_updated_at = payload.get("stage_cursor_after_updated_at")
        after_task_id = str(payload.get("stage_cursor_after_task_id") or "")
        resume_current_stage = bool(payload.get("current_stage_main_cleanup_complete"))
        if not resume_current_stage:
            payload.update({
                "overall_processed_projects": int(payload.get("stage_baseline_projects") or 0),
                "overall_processed_files": int(payload.get("stage_baseline_files") or 0),
                "overall_archived_bytes": int(payload.get("stage_baseline_archived_bytes") or 0),
                "processed_projects": int(payload.get("stage_baseline_projects") or 0),
                "processed_files": int(payload.get("stage_baseline_files") or 0),
                "archived_bytes": int(payload.get("stage_baseline_archived_bytes") or 0),
                "archived": int(payload.get("stage_baseline_archived") or 0),
                "archived_projects": int(payload.get("stage_baseline_archived_projects") or 0),
                "bytes_freed": int(payload.get("stage_baseline_bytes_freed") or 0),
                "main_bytes_freed": int(payload.get("stage_baseline_main_bytes_freed") or 0),
                "worker_bytes_freed": int(payload.get("stage_baseline_worker_bytes_freed") or 0),
                "error_count": int(payload.get("stage_baseline_error_count") or 0),
                "errors": list(payload.get("stage_baseline_errors") or []),
                "worker_cleanup_task_ids": [],
                "current_project_task_id": "",
                "current_asset": "",
                "current_asset_size_bytes": 0,
                "current_asset_uploaded_bytes": 0,
                "current_asset_percent": 0,
            })
    if (after_updated_at is None) != (not after_task_id):
        raise HTTPException(409, "O ponto seguro de retomada não está completo. Inicie um novo backup para enviar as pendências.")
    payload.update({
        "status": "queued",
        "pause_requested": False,
        "resume_available": False,
        "requested_limit": _MIGRATION_BATCH_SIZE,
        "batch_size": _MIGRATION_BATCH_SIZE,
        "batch_duration_target_seconds": _MIGRATION_BATCH_REFERENCE_SECONDS,
    })
    _write_job(job_id, payload)
    try:
        stage_job_id = _queue_migration_stage(
            job_id,
            int(current_user.id),
            after_updated_at,
            after_task_id,
            resume_current_stage=resume_current_stage,
        )
    except Exception as exc:
        payload = _read_job(job_id)
        payload.update({"status": "interrupted", "resume_available": True})
        _write_job(job_id, payload)
        raise HTTPException(503, "A fila do worker está indisponível. Os arquivos sem confirmação foram mantidos.") from exc
    return {"job_id": job_id, "status": "queued", "stage_job_id": stage_job_id}


@router.get("/migrate/latest")
def latest_google_drive_migration(current_user: User = Depends(get_current_admin_user)):
    latest_payload = None
    latest_modified = -1.0
    for path in _migration_dir().glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            owner_id = payload.get("created_by")
            if owner_id is not None and int(owner_id) != int(current_user.id):
                continue
            modified = path.stat().st_mtime
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if modified > latest_modified:
            latest_payload = payload
            latest_modified = modified
    if latest_payload is None:
        raise HTTPException(404, "Nenhum backup do Google Drive foi encontrado.")
    return latest_payload


@router.get("/migrate/{job_id}")
def google_drive_migration_status(job_id: str, current_user: User = Depends(get_current_admin_user)):
    payload = _read_job(job_id)
    stage_job_id = str(payload.get("stage_job_id") or "").strip()
    status = str(payload.get("status") or "").lower()
    if stage_job_id and status in {"queued", "processing"}:
        try:
            from rq.job import Job
            from app.redis_client import conn

            if conn is None:
                payload["stage_job_status"] = "unavailable"
            else:
                worker_job = Job.fetch(stage_job_id, connection=conn)
                rq_status = worker_job.get_status(refresh=True)
                rq_status = str(getattr(rq_status, "value", rq_status) or "unknown").lower()
                payload["stage_job_status"] = rq_status
                if rq_status in {"failed", "stopped", "canceled", "cancelled"}:
                    errors = list(payload.get("errors") or [])
                    errors.append("A etapa do worker foi interrompida. Os arquivos sem confirmação foram mantidos.")
                    payload.update({
                        "status": "interrupted",
                        "stage_phase": "interrupted",
                        "resume_available": True,
                        "errors": errors[-50:],
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    })
                    _write_job(job_id, payload)
                elif rq_status == "finished" and status in {"queued", "processing"}:
                    last_update = _timestamp(payload.get("status_updated_at"), 0.0)
                    if last_update and time.time() - last_update > 10 * 60:
                        errors = list(payload.get("errors") or [])
                        errors.append("O worker concluiu a etapa sem registrar a próxima. As cópias não confirmadas foram mantidas.")
                        payload.update({
                            "status": "interrupted",
                            "stage_phase": "interrupted",
                            "resume_available": True,
                            "errors": errors[-50:],
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        })
                        _write_job(job_id, payload)
        except Exception:
            # Redis status is supplemental; stored progress remains available.
            payload["stage_job_status"] = "unavailable"
    elif not stage_job_id and status in {"queued", "processing"}:
        # Old in-process migrations stop updating their JSON when the web
        # process is redeployed. Report them as interrupted after a long idle.
        last_update = _timestamp(payload.get("status_updated_at"), 0.0)
        if last_update and time.time() - last_update > 60 * 60:
            errors = list(payload.get("errors") or [])
            errors.append("Este backup antigo parou de atualizar. Inicie um novo backup para enviar apenas as pendências; cópias sem confirmação permanecem locais.")
            payload.update({
                "status": "interrupted",
                "stage_phase": "interrupted",
                "resume_available": True,
                "errors": errors[-50:],
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            _write_job(job_id, payload)
    _refresh_migration_timing(payload)
    worker_job_id = str(payload.get("worker_cleanup_job_id") or "").strip()
    if stage_job_id or not worker_job_id:
        return payload
    if payload.get("worker_cleanup_status") in {"finished", "failed", "stopped", "canceled", "cancelled"}:
        return payload
    try:
        from rq.job import Job
        from app.redis_client import conn

        if conn is None:
            payload["worker_cleanup_status"] = "unavailable"
            return payload
        worker_job = Job.fetch(worker_job_id, connection=conn)
        status = worker_job.get_status(refresh=True)
        status = str(getattr(status, "value", status) or "unknown").lower()
        payload["worker_cleanup_status"] = status
        if status == "finished":
            result = worker_job.result
            if isinstance(result, dict):
                payload["worker_cleanup_result"] = result
                if not result.get("enabled", True):
                    payload["worker_cleanup_message"] = (
                        "O CX33 não está configurado para consultar o arquivo central; nenhum arquivo dele foi removido."
                    )
                elif int(result.get("errors") or 0) > 0:
                    payload["worker_cleanup_message"] = (
                        "Alguns arquivos do CX33 não puderam ser verificados e permaneceram no disco."
                    )
        elif status in {"failed", "stopped", "canceled", "cancelled"}:
            payload["worker_cleanup_message"] = (
                "A limpeza do CX33 não terminou; os arquivos ainda presentes foram mantidos."
            )
        if payload.get("worker_cleanup_status") in {"finished", "failed", "stopped", "canceled", "cancelled"}:
            _write_job(job_id, payload)
    except Exception:
        # Redis may be temporarily unreachable while the RQ worker continues.
        # Keep this state nonterminal so the page will retry its status poll.
        payload["worker_cleanup_status"] = "unavailable"
    return payload
