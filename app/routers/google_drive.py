"""OAuth connection and controlled migration to the Google Drive archive."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
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
from app.models import Settings, User
from app.routers.auth import SECRET_KEY, get_current_admin_user
from app.routers.settings import _get_or_create_settings_row
from app.services.central_asset_transfer import digest, register, roots
from app.services.google_drive_archive import (
    DRIVE_SCOPE,
    GoogleDriveArchive,
    GoogleDriveArchiveError,
    ROOT_FOLDER_NAME,
    encrypt_refresh_token,
)

router = APIRouter(prefix="/settings/google-drive", tags=["Google Drive archive"])
_STATE = URLSafeTimedSerializer(SECRET_KEY, salt="codexia-google-drive-oauth-v1")
_ACTIVE_STATUSES = {"processing", "running", "queued", "pending", "in_progress", "executing"}
_MIGRATION_EXTENSIONS = {
    "video": {".mp4", ".mov", ".mkv", ".webm"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    "image": {".png", ".jpg", ".jpeg", ".webp"},
}


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


@router.get("/auth-url")
def google_drive_auth_url(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    settings = _get_or_create_settings_row(db, user_id=current_user.id)
    client_id, client_secret = _oauth_credentials(settings)
    redirect_uri = _callback_uri(request)
    if not redirect_uri.startswith("https://") and "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        raise HTTPException(400, "O Google Drive exige callback HTTPS. Configure GOOGLE_DRIVE_OAUTH_REDIRECT_URI com a URL HTTPS pública do app.")
    state = _STATE.dumps({"user_id": int(current_user.id), "redirect_uri": redirect_uri, "nonce": uuid.uuid4().hex})
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
            access_type="offline", prompt="consent", include_granted_scopes="true", state=state,
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
        if credentials.refresh_token:
            settings.google_drive_refresh_token = encrypt_refresh_token(credentials.refresh_token)
        elif not getattr(settings, "google_drive_refresh_token", None):
            raise RuntimeError("O Google não retornou refresh token. Refaça a autorização com consentimento.")
        settings.google_drive_folder_id = None
        db.add(settings)
        db.commit()
        db.refresh(settings)
        GoogleDriveArchive(user_id=user_id).root_folder_id()
        from app.services.server_resource_monitor import invalidate_drive_usage_cache

        invalidate_drive_usage_cache(user_id)
    except Exception as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, (RuntimeError, GoogleDriveArchiveError)) else "Falha ao trocar o código OAuth. Confira o callback HTTPS cadastrado no Google Cloud."
        return HTMLResponse(f"<h2>Falha ao conectar Google Drive</h2><p>{escape(message)}</p><p>Feche esta aba e revise as Configurações do Google OAuth.</p>", status_code=400)
    return HTMLResponse(
        "<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Google Drive conectado</title><body style='font:16px system-ui;max-width:640px;margin:12vh auto;padding:24px;color:#172033'>"
        "<h2>Google Drive conectado</h2><p>A pasta <b>Codexia Archive</b> está pronta. Os próximos ativos de produção serão enviados diretamente ao Drive.</p>"
        "<p>Feche esta aba e atualize o status nas Configurações. A migração de arquivos antigos é separada.</p></body></html>"
    )


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


def _migration_candidates(limit: int) -> list[tuple[str, Path, str]]:
    from app.services.task_manager import get_task

    cutoff = time.time() - 24 * 60 * 60
    candidates = []
    seen_names = set()
    roots_by_kind = {kind: [Path(roots()[kind]).resolve()] for kind in ("video", "audio", "image")}
    try:
        from app.config import COVERS_OUTPUT_DIR
        cover_root = Path(COVERS_OUTPUT_DIR).resolve()
        if cover_root not in roots_by_kind["image"]:
            roots_by_kind["image"].append(cover_root)
    except Exception:
        pass
    for kind, kind_roots in roots_by_kind.items():
        for root in kind_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if (not path.is_file() or path.is_symlink() or path.name.startswith(".")
                        or path.suffix.lower() not in _MIGRATION_EXTENSIONS[kind]):
                    continue
                try:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(root) or path.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
                if (kind, path.name) in seen_names:
                    continue
                # Never migrate files belonging to an active task.
                try:
                    relative = resolved.relative_to(root)
                    if len(relative.parts) > 1:
                        task = get_task(relative.parts[0]) or {}
                        if str(task.get("status") or "").lower() in _ACTIVE_STATUSES:
                            continue
                except Exception:
                    pass
                seen_names.add((kind, path.name))
                candidates.append((kind, path, str(resolved)))
    candidates.sort(key=lambda item: item[1].stat().st_size if item[1].exists() else 0, reverse=True)
    return candidates[:limit]


def _run_migration(job_id: str, user_id: int, limit: int) -> None:
    payload = _read_job(job_id)
    payload.update({"status": "processing", "started_at": datetime.now(timezone.utc).isoformat()})
    _write_job(job_id, payload)
    archive = GoogleDriveArchive(user_id=user_id)
    archived = 0
    skipped = 0
    errors = []
    bytes_freed = 0
    try:
        candidates = _migration_candidates(limit)
        payload["total_candidates"] = len(candidates)
        _write_job(job_id, payload)
        for kind, path, stable_path in candidates:
            original_size = path.stat().st_size if path.exists() else 0
            legacy_task = "legacy-" + hashlib.sha256(stable_path.encode("utf-8")).hexdigest()[:24]
            try:
                source_sha = digest(path)
                entry = archive.upload_path(legacy_task, kind, path.name, path)
                if digest(path) != source_sha:
                    skipped += 1
                    continue
                register(legacy_task, entry)
                path.unlink()
                archived += 1
                bytes_freed += original_size
            except Exception as exc:
                errors.append(f"{path.name}: {str(exc)[:200]}")
            payload.update({
                "archived": archived,
                "skipped": skipped,
                "bytes_freed": bytes_freed,
                "errors": errors[-10:],
            })
            _write_job(job_id, payload)
    except Exception as exc:
        errors.append(str(exc)[:240])
    payload.update({
        "status": "completed" if not errors else ("completed_with_errors" if archived or skipped else "failed"),
        "archived": archived,
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
        "archived": 0, "skipped": 0, "bytes_freed": 0, "errors": [],
    })
    background_tasks.add_task(_run_migration, job_id, int(current_user.id), limit)
    return {"job_id": job_id, "status": "queued", "message": "Migração iniciada. Arquivos ativos ou alterados nas últimas 24 horas serão ignorados."}


@router.get("/migrate/{job_id}")
def google_drive_migration_status(job_id: str, current_user: User = Depends(get_current_admin_user)):
    return _read_job(job_id)
