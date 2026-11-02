"""Metadata and recovery endpoints for worker-to-Google-Drive transfers."""
import hmac
import os
from pathlib import Path
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from app.services.central_asset_transfer import central_index, task_key
from app.services.google_drive_archive import GoogleDriveArchive

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
    for entry in central_index(task, verify=False):
        if entry['kind'] == kind and entry['filename'] == filename:
            if entry.get('path') and Path(entry['path']).is_file():
                return FileResponse(entry['path'])
            if entry.get('drive_file_id'):
                return GoogleDriveArchive.for_asset(entry).stream_response(entry)
    raise HTTPException(404, 'Ativo não arquivado nesta produção')


@router.put('/{task}/{kind}/{filename}', dependencies=[Depends(authenticate)])
def reject_relay_upload(task: str, kind: str, filename: str):
    # Deliberately reject old worker clients before consuming the request body.
    # New clients ask the control plane for a Google resumable-session URL and
    # send all media bytes from CX33 straight to Google Drive.
    raise HTTPException(
        status_code=410,
        detail='Transferência de mídia pelo servidor principal desativada. Atualize o worker para o upload direto ao Google Drive.',
    )
