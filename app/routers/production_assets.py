"""Private machine-to-machine archive endpoints, disabled without an archive role."""
import hashlib
import hmac
import os
import tempfile
import threading
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from contextlib import contextmanager
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from app.services.central_asset_transfer import archive_path, central_index, digest, register, task_key
from app.services.google_drive_archive import (
    DriveAssetConflict,
    GoogleDriveArchive,
    GoogleDriveArchiveError,
)

router = APIRouter(prefix='/internal/production-assets', include_in_schema=False)
_ARCHIVE_WRITE_LOCK = threading.RLock()
_ASYNC_FALLBACK_LOCKS = {}
_ASYNC_FALLBACK_LOCKS_GUARD = threading.Lock()


@contextmanager
def _archive_write_lock(path: Path):
    """Serialize compare-and-swap replacement across threads and API workers."""
    lock_path = path.parent / '.asset-archive.lock'
    with _ARCHIVE_WRITE_LOCK:
        with lock_path.open('a+b') as lock_file:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows development fallback
                yield
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def _archive_write_lock_async(path: Path):
    """Hold the same cross-process lock without blocking the async upload loop."""
    lock_path = path.parent / '.asset-archive.lock'
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows development fallback
        key = str(lock_path.resolve())
        with _ASYNC_FALLBACK_LOCKS_GUARD:
            lock = _ASYNC_FALLBACK_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            yield
        return

    lock_file = lock_path.open('a+b')
    try:
        await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def authenticate(x_codexia_asset_token: str = Header(default='')):
    token = os.getenv('CODEXIA_ASSET_TRANSFER_TOKEN') or ''
    if os.getenv('CODEXIA_ASSET_ARCHIVE_ROLE') != 'archive' or not token:
        raise HTTPException(404)
    if not hmac.compare_digest(token, x_codexia_asset_token):
        raise HTTPException(401, 'Acesso ao arquivo central negado')


@router.get('/{task}', dependencies=[Depends(authenticate)])
def files(task: str):
    try:
        task_key(task)
        return {'files': [{k: v for k, v in entry.items() if k != 'path'} for entry in central_index(task)]}
    except ValueError:
        raise HTTPException(400, 'ID inválido')


@router.get('/{task}/{kind}/{filename}', dependencies=[Depends(authenticate)])
def download(task: str, kind: str, filename: str):
    for entry in central_index(task, verify=False):
        if entry['kind'] == kind and entry['filename'] == filename:
            if entry.get('path') and Path(entry['path']).is_file():
                return FileResponse(entry['path'])
            if entry.get('drive_file_id'):
                return GoogleDriveArchive().stream_response(entry)
    raise HTTPException(404, 'Ativo não arquivado nesta produção')


@router.put('/{task}/{kind}/{filename}', dependencies=[Depends(authenticate)])
async def upload(task: str, kind: str, filename: str, request: Request,
                 x_content_sha256: str = Header(default=''),
                 x_expected_previous_sha256: str = Header(default=''),
                 x_content_length: int = Header(default=0)):
    try:
        task_key(task)
        path = archive_path(kind, filename, task=task)
    except ValueError:
        raise HTTPException(400, 'Ativo inválido')
    if len(x_content_sha256) != 64 or any(c not in '0123456789abcdef' for c in x_content_sha256):
        raise HTTPException(400, 'SHA256 obrigatório')
    if x_expected_previous_sha256 and (
        len(x_expected_previous_sha256) != 64
        or any(c not in '0123456789abcdef' for c in x_expected_previous_sha256)
    ):
        raise HTTPException(400, 'SHA256 anterior inválido')

    # Once a Drive account is connected, stream chunks directly to its
    # resumable-upload session. No full-size media file is written to /data.
    drive = GoogleDriveArchive()
    try:
        drive_configured = drive.is_configured()
    except GoogleDriveArchiveError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if drive_configured:
        max_bytes = int(os.getenv('CODEXIA_ASSET_MAX_BYTES', str(4 * 1024**3)))
        if x_content_length <= 0:
            raise HTTPException(411, 'Tamanho do ativo obrigatório para upload direto ao Google Drive')
        if x_content_length > max_bytes:
            raise HTTPException(413, 'Ativo excede o limite de transferência')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            async with _archive_write_lock_async(path):
                entry = await drive.upload_stream(
                    task, kind, filename, request.stream(), x_content_sha256,
                    x_content_length, x_expected_previous_sha256,
                )
                register(task, entry)
        except DriveAssetConflict as exc:
            raise HTTPException(status_code=409, detail={
                'code': 'ASSET_CONTENT_CONFLICT', 'existing_sha256': exc.existing_sha256,
                'message': str(exc),
            })
        except (GoogleDriveArchiveError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return entry

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.upload')
    tmp = Path(temporary)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, 'wb') as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > int(os.getenv('CODEXIA_ASSET_MAX_BYTES', str(4 * 1024**3))):
                    raise HTTPException(413, 'Ativo excede o limite de transferência')
                h.update(chunk)
                stream.write(chunk)
        if not size or h.hexdigest() != x_content_sha256:
            raise HTTPException(422, 'Integridade do arquivo não confere')
        with _archive_write_lock(path):
            existing_sha = digest(path) if path.is_file() else ''
            if existing_sha and existing_sha != x_content_sha256 and existing_sha != x_expected_previous_sha256:
                raise HTTPException(409, detail={
                    'code': 'ASSET_CONTENT_CONFLICT',
                    'existing_sha256': existing_sha,
                    'message': 'O nome já contém outro conteúdo; a substituição exige confirmação pela versão atual.',
                })
            os.replace(tmp, path)
            entry = {'kind': kind, 'filename': filename, 'path': str(path), 'sha256': h.hexdigest(), 'size': size}
            register(task, entry)
        return {k: v for k, v in entry.items() if k != 'path'}
    finally:
        tmp.unlink(missing_ok=True)
