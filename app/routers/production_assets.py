"""Private machine-to-machine archive endpoints, disabled without an archive role."""
import hashlib
import hmac
import os
import tempfile
from pathlib import Path
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from app.services.central_asset_transfer import archive_path, central_index, digest, register, task_key

router = APIRouter(prefix='/internal/production-assets', include_in_schema=False)


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
    for entry in central_index(task):
        if entry['kind'] == kind and entry['filename'] == filename:
            return FileResponse(entry['path'])
    raise HTTPException(404, 'Ativo não arquivado nesta produção')


@router.put('/{task}/{kind}/{filename}', dependencies=[Depends(authenticate)])
async def upload(task: str, kind: str, filename: str, request: Request,
                 x_content_sha256: str = Header(default='')):
    try:
        task_key(task)
        path = archive_path(kind, filename)
    except ValueError:
        raise HTTPException(400, 'Ativo inválido')
    if len(x_content_sha256) != 64 or any(c not in '0123456789abcdef' for c in x_content_sha256):
        raise HTTPException(400, 'SHA256 obrigatório')
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
        if path.exists() and digest(path) != x_content_sha256:
            raise HTTPException(409, 'Nome já utilizado por outro conteúdo; arquivo preservado')
        os.replace(tmp, path)
        entry = {'kind': kind, 'filename': filename, 'path': str(path), 'sha256': h.hexdigest(), 'size': size}
        register(task, entry)
        return {k: v for k, v in entry.items() if k != 'path'}
    finally:
        tmp.unlink(missing_ok=True)
