"""
Backup automático do SQLite. Usa sqlite3.Connection.backup() para backup consistente.
Só roda quando o banco é SQLite (não Postgres).
"""
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional
from pathlib import Path

from app.database import SQLALCHEMY_DATABASE_URL

logger = logging.getLogger(__name__)

BACKUP_DIR = "/data/backups"
KEEP_COUNT = 7
BACKUP_PREFIX = "vibraface_"


def _get_sqlite_path():
    """Retorna o path do arquivo SQLite ou None se for Postgres."""
    if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        return None
    # sqlite:////data/vibraface.db -> /data/vibraface.db
    url = SQLALCHEMY_DATABASE_URL
    if url.startswith("sqlite:///"):
        path = url[10:]  # remove "sqlite:///"
        if path.startswith("/"):
            return path
        return "/" + path
    return None


def run_sqlite_backup():
    """Executa backup do SQLite usando API nativa (consistente). Mantém últimos 7."""
    db_path = _get_sqlite_path()
    if not db_path or not os.path.exists(db_path):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{ts}.db")
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        logger.info(f"Backup SQLite criado: {backup_path}")
        _prune_old_backups()
    except Exception as e:
        logger.error(f"Erro ao criar backup SQLite: {e}")


def _prune_old_backups():
    """Remove backups antigos, mantendo apenas os últimos KEEP_COUNT."""
    if not os.path.isdir(BACKUP_DIR):
        return
    files = []
    for f in os.listdir(BACKUP_DIR):
        if f.startswith(BACKUP_PREFIX) and f.endswith(".db"):
            path = os.path.join(BACKUP_DIR, f)
            files.append((path, os.path.getmtime(path)))
    files.sort(key=lambda x: x[1], reverse=True)
    for path, _ in files[KEEP_COUNT:]:
        try:
            os.remove(path)
            logger.info(f"Backup antigo removido: {path}")
        except Exception as e:
            logger.warning(f"Erro ao remover backup antigo {path}: {e}")


def list_backups():
    """Lista backups disponíveis: nome, data (mtime), tamanho em bytes."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for f in os.listdir(BACKUP_DIR):
        if f.startswith(BACKUP_PREFIX) and f.endswith(".db"):
            path = os.path.join(BACKUP_DIR, f)
            try:
                st = os.stat(path)
                items.append({
                    "name": f,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "size": st.st_size,
                })
            except OSError:
                pass
    items.sort(key=lambda x: x["modified"], reverse=True)
    return items


def get_backup_path(filename: str) -> Optional[str]:
    """Retorna o path absoluto do backup se for válido e existir, else None."""
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return None
    if not filename.startswith(BACKUP_PREFIX) or not filename.endswith(".db"):
        return None
    path = os.path.join(BACKUP_DIR, filename)
    return path if os.path.isfile(path) else None
