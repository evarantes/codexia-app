"""Private per-user visual reference storage for Entre Nós Dois.

No public StaticFiles mount; originals are only returned through admin-authenticated
routes. Configure CODEXIA_STUDIO_PRIVATE_DIR on a persistent mounted volume.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
HOST_NAMES = {"Gabriel", "Helena"}


class StudioReferenceError(ValueError):
    pass


def _storage_root() -> Path:
    raw = os.getenv("CODEXIA_STUDIO_PRIVATE_DIR", "").strip()
    if not raw:
        raise StudioReferenceError("Configure CODEXIA_STUDIO_PRIVATE_DIR em volume persistente.")
    root = Path(raw).expanduser().resolve()
    if not root.is_absolute():
        raise StudioReferenceError("Diretório privado inválido.")
    public = (
        Path(__file__).resolve().parents[1] / "static",
        Path(__file__).resolve().parents[2] / "generated_assets",
    )
    if any(root == p.resolve() or root.is_relative_to(p.resolve()) for p in public):
        raise StudioReferenceError("O armazenamento privado não pode ficar em diretório público.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def reference_file(*, user_id: int, host: str) -> Path:
    if type(user_id) is not int or user_id <= 0 or host not in HOST_NAMES:
        raise StudioReferenceError("Usuário ou personagem inválido.")
    return _storage_root() / f"user-{user_id}" / "entre-nos-dois" / f"{host.lower()}.png"


def store_host_reference(*, user_id: int, host: str, body: bytes) -> dict:
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_IMAGE_BYTES:
        raise StudioReferenceError("Imagem inválida ou acima de 8 MB.")
    try:
        with Image.open(io.BytesIO(body)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP"):
                raise StudioReferenceError("Formato não permitido. Use JPG, PNG ou WebP.")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise StudioReferenceError("Imagem excede o limite de resolução.")
            image.load()
            converted = image.convert("RGB")
            out = io.BytesIO()
            converted.save(out, format="PNG", optimize=True)
            data = out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise StudioReferenceError("Arquivo não é uma imagem válida.") from None
    path = reference_file(user_id=user_id, host=host)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".uploading")
    try:
        with tmp.open("wb") as dest:
            os.chmod(tmp, 0o600)
            dest.write(data)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return {"host": host, "stored": True, "private": True, "bytes": len(data)}


def host_reference_status(*, user_id: int) -> dict:
    return {
        host: reference_file(user_id=user_id, host=host).is_file()
        for host in sorted(HOST_NAMES)
    }


def private_storage_health() -> dict:
    """Verify actual write access without exposing paths or creating paid work.

    This does not prove that the directory is mounted persistently. That still
    requires an operator to check the Coolify volume configuration.
    """
    raw = os.getenv("CODEXIA_STUDIO_PRIVATE_DIR", "").strip()
    if not raw:
        return {"ready": False, "reason": "missing_configuration"}
    try:
        root = _storage_root()
        with tempfile.NamedTemporaryFile(prefix=".studio-writecheck-", dir=root, delete=True) as file:
            file.write(b"ok")
            file.flush()
            os.fsync(file.fileno())
    except StudioReferenceError as exc:
        return {"ready": False, "reason": "public_directory" if "diretório público" in str(exc) else "not_writable"}
    except (OSError, ValueError):
        return {"ready": False, "reason": "not_writable"}
    return {"ready": True, "reason": "write_access_verified"}
