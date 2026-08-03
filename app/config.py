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

def _dir_is_writable(path: str) -> bool:
    try:
        if os.name == "nt":
            return False
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".__codexia_write_probe_{os.getpid()}")
        with open(probe, "wb") as f:
            f.write(b"1")
        os.remove(probe)
        return True
    except Exception:
        return False

def _env_truthy(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in ("1", "true", "yes", "on")

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
_data_video_dir = os.path.join("/data", "media", "videos")
_use_data_volume = os.path.isdir("/data") and not _env_truthy("USE_STATIC_VIDEOS") and _dir_is_writable(_data_video_dir)
VIDEO_OUTPUT_DIR = _data_video_dir if _use_data_volume else str(STATIC_DIR / "videos")
VIDEO_URL_PREFIX = "/media/videos" if _use_data_volume else "/static/videos"
try:
    os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)
except Exception:
    pass

# Diretório de saída para músicas (Suno/MusicGen).
# Com volume /data (Coolify etc.): usar /data/media/music para persistir e compartilhar entre instâncias.
_data_music_dir = os.path.join("/data", "media", "music")
_use_data_volume_music = os.path.isdir("/data") and not _env_truthy("USE_STATIC_MUSIC") and _dir_is_writable(_data_music_dir)
MUSIC_OUTPUT_DIR = _data_music_dir if _use_data_volume_music else str(STATIC_DIR / "music")
MUSIC_URL_PREFIX = "/media/music" if _use_data_volume_music else "/static/music"
try:
    os.makedirs(MUSIC_OUTPUT_DIR, exist_ok=True)
except Exception:
    pass

_data_books_dir = os.path.join("/data", "media", "books")
_data_covers_dir = os.path.join("/data", "media", "covers")
_use_data_volume_books = os.path.isdir("/data") and not _env_truthy("USE_STATIC_BOOKS") and _dir_is_writable(_data_books_dir) and _dir_is_writable(_data_covers_dir)
BOOKS_OUTPUT_DIR = _data_books_dir if _use_data_volume_books else str(STATIC_DIR / "books")
COVERS_OUTPUT_DIR = _data_covers_dir if _use_data_volume_books else str(STATIC_DIR / "covers")
try:
    os.makedirs(BOOKS_OUTPUT_DIR, exist_ok=True)
except Exception:
    pass
try:
    os.makedirs(COVERS_OUTPUT_DIR, exist_ok=True)
except Exception:
    pass

def absolute_path_for_music(filename_or_url: str) -> str:
    if not filename_or_url:
        return ""
    clean = (filename_or_url or "").strip().split("?", 1)[0].split("#", 1)[0].lstrip("/")
    name = os.path.basename(clean)
    if not name:
        name = clean
    candidates = [
        os.path.join(MUSIC_OUTPUT_DIR, name),
        str(STATIC_DIR / "music" / name),
        os.path.join("app", "static", "music", name),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return os.path.join(MUSIC_OUTPUT_DIR, name)
