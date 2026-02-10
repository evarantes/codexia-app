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
    """Path absoluto para vídeo. Procura em /data e em app/static/videos (Render/sem disco)."""
    if not url_path:
        return ""
    clean = (url_path or "").strip().lstrip("/")
    name = os.path.basename(clean)
    if not name and "videos/" in clean:
        name = clean.split("videos/", 1)[-1].strip("/").split("/")[0] or clean
    if not name:
        name = clean
    candidates = [
        os.path.join("/data", "media", "videos", name),
        str(STATIC_DIR / "videos" / name),
    ]
    if clean.startswith("media/"):
        candidates.insert(0, os.path.join("/data", clean))
    if clean.startswith("static/"):
        candidates.insert(0, str(STATIC_DIR / clean[7:]))
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return str(STATIC_DIR / "videos" / name)


# Diretório de saída para vídeos.
# Em Render/sem disco persistente: usar app/static/videos (USE_STATIC_VIDEOS=1 ou sem /data).
# Com volume /data (Coolify etc.): usar /data/media/videos.
_use_data_volume = os.path.isdir("/data") and os.getenv("USE_STATIC_VIDEOS", "").lower() not in ("1", "true", "yes")
VIDEO_OUTPUT_DIR = "/data/media/videos" if _use_data_volume else str(STATIC_DIR / "videos")
VIDEO_URL_PREFIX = "/media/videos" if _use_data_volume else "/static/videos"
