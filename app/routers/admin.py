"""Rotas administrativas protegidas (requer is_admin)."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.routers.auth import get_current_admin_user
from app.models import User
from app.services import backup_service

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/backups")
def list_backups(admin: User = Depends(get_current_admin_user)):
    """Lista backups disponíveis (nome, data, tamanho)."""
    items = backup_service.list_backups()
    return {"backups": items}


@router.get("/backups/{filename}")
def download_backup(filename: str, admin: User = Depends(get_current_admin_user)):
    """Baixa um arquivo de backup por nome."""
    path = backup_service.get_backup_path(filename)
    if not path:
        raise HTTPException(status_code=404, detail="Backup não encontrado")
    return FileResponse(path, filename=filename)
