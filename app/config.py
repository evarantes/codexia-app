"""
Configurações centralizadas para caminhos e URLs.
Compatível com Docker/Coolify: não usa caminhos do Windows nem localhost fixo.
"""
import os
from pathlib import Path

# Raiz do projeto (pasta que contém app/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Pasta estática (app/static)
STATIC_DIR = PROJECT_ROOT / "app" / "static"

# URL base da aplicação (para links em emails, callbacks de pagamento, etc.)
# Em produção: definir BASE_URL no ambiente (ex.: https://seu-dominio.com)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

def path_from_static_url(url_path: str) -> Path:
    """Converte um path de URL estática (/static/...) para path no disco."""
    if not url_path:
        return STATIC_DIR
    clean = url_path.lstrip("/")
    if clean.startswith("static/"):
        clean = clean[7:]  # remove "static/"
    return STATIC_DIR / clean if clean else STATIC_DIR

def absolute_path_for_static(url_path: str) -> str:
    """Retorna path absoluto no disco para uma URL /static/... (compatível com Docker)."""
    return str(path_from_static_url(url_path))


def absolute_path_for_video(url_path: str) -> str:
    """Path absoluto para vídeo: /static/videos/ ou /media/videos/ (volume /data)."""
    if not url_path:
        return ""
    clean = (url_path or "").strip().lstrip("/")
    if clean.startswith("media/"):
        return os.path.join("/data", clean)
    if clean.startswith("static/"):
        return str(STATIC_DIR / clean[7:])
    return str(path_from_static_url(url_path))


# Diretório de saída para vídeos: /data/media/videos em container, app/static/videos localmente
VIDEO_OUTPUT_DIR = "/data/media/videos" if os.path.isdir("/data") else str(STATIC_DIR / "videos")
VIDEO_URL_PREFIX = "/media/videos" if os.path.isdir("/data") else "/static/videos"
