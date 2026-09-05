from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import AUDIO_OUTPUT_DIR


class ProductionJobStoreError(RuntimeError):
    pass


class ProductionJobStore:
    """Fonte de verdade física para uma produção narrada.

    Cada trabalho possui uma pasta própria dentro do namespace do Narration Core,
    evitando depender de cache/localStorage para localizar o MP3 aprovado.
    """

    def __init__(self, output_root: str | None = None):
        core_root = Path(output_root or AUDIO_OUTPUT_DIR) / "youtube_narration_core_v1"
        self.root = core_root / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _safe_job_id(value: Any) -> str:
        job_id = str(value or "").strip()
        if not job_id or len(job_id) > 64 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in job_id):
            raise ProductionJobStoreError("Código de trabalho inválido.")
        return job_id

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _user_root(self, user_id: int) -> Path:
        path = self.root / str(max(0, int(user_id or 0)))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _job_dir(self, user_id: int, job_id: str) -> Path:
        safe = self._safe_job_id(job_id)
        path = (self._user_root(user_id) / safe).resolve()
        try:
            path.relative_to(self._user_root(user_id).resolve())
        except Exception as exc:
            raise ProductionJobStoreError("Pasta de trabalho inválida.") from exc
        return path

    def create_or_get(self, *, user_id: int, job_id: Optional[str] = None, theme: str = "") -> Dict[str, Any]:
        if job_id:
            path = self._job_dir(user_id, job_id)
            meta_path = path / "job.json"
            meta = self._read_json(meta_path)
            if not meta:
                raise ProductionJobStoreError("Trabalho não encontrado.")
            return meta

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        new_id = f"YT-{stamp}-{uuid.uuid4().hex[:10]}"
        path = self._job_dir(user_id, new_id)
        path.mkdir(parents=True, exist_ok=False)
        meta = {
            "job_id": new_id,
            "user_id": int(user_id),
            "theme": str(theme or "").strip()[:500],
            "status": "awaiting_narration_review",
            "created_at": self._now(),
            "updated_at": self._now(),
            "narration_versions": [],
            "approved_audio_path": "",
            "approved_audio_sha256": "",
            "approved_preview_id": "",
            "tts_locked": False,
        }
        self._write_json(path / "job.json", meta)
        (path / "images").mkdir(exist_ok=True)
        return meta

    def register_preview(
        self,
        *,
        user_id: int,
        source_mp3: Path,
        source_meta: Path,
        preview_id: str,
        theme: str = "",
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not source_mp3.is_file() or source_mp3.stat().st_size <= 512 or not source_meta.is_file():
            raise ProductionJobStoreError("MP3/metadados da narração não estão disponíveis para o trabalho.")
        job = self.create_or_get(user_id=user_id, job_id=job_id, theme=theme)
        job_dir = self._job_dir(user_id, job["job_id"])
        version = len(job.get("narration_versions") or []) + 1
        mp3_target = job_dir / f"narracao_v{version}.mp3"
        json_target = job_dir / f"narracao_v{version}.json"
        shutil.copy2(source_mp3, mp3_target)
        shutil.copy2(source_meta, json_target)
        entry = {
            "version": version,
            "preview_id": str(preview_id),
            "audio_path": str(mp3_target.resolve()),
            "meta_path": str(json_target.resolve()),
            "audio_sha256": self._sha256_file(mp3_target),
            "created_at": self._now(),
        }
        versions = list(job.get("narration_versions") or [])
        versions.append(entry)
        job["narration_versions"] = versions
        job["current_preview_id"] = str(preview_id)
        job["current_audio_path"] = entry["audio_path"]
        job["status"] = "awaiting_narration_review"
        job["updated_at"] = self._now()
        self._write_json(job_dir / "job.json", job)
        return job

    def approve_preview(self, *, user_id: int, job_id: str, preview_id: str) -> Dict[str, Any]:
        job = self.create_or_get(user_id=user_id, job_id=job_id)
        versions = list(job.get("narration_versions") or [])
        selected = next((v for v in reversed(versions) if str(v.get("preview_id") or "") == str(preview_id)), None)
        if not selected:
            raise ProductionJobStoreError("A narração não pertence a este trabalho.")
        src_mp3 = Path(str(selected.get("audio_path") or ""))
        src_json = Path(str(selected.get("meta_path") or ""))
        if not src_mp3.is_file() or src_mp3.stat().st_size <= 512 or not src_json.is_file():
            raise ProductionJobStoreError("A versão escolhida da narração não está íntegra.")
        meta = self._read_json(src_json)
        if meta.get("approved") is not True or str(meta.get("preview_id") or "") != str(preview_id):
            raise ProductionJobStoreError("A narração ainda não foi aprovada pelo Narration Core.")

        job_dir = self._job_dir(user_id, job_id)
        approved_mp3 = job_dir / "approved_narration.mp3"
        approved_json = job_dir / "approved_narration.json"
        shutil.copy2(src_mp3, approved_mp3)
        shutil.copy2(src_json, approved_json)
        audio_sha = self._sha256_file(approved_mp3)
        job.update({
            "status": "narration_approved",
            "approved_preview_id": str(preview_id),
            "approved_audio_path": str(approved_mp3.resolve()),
            "approved_audio_meta_path": str(approved_json.resolve()),
            "approved_audio_sha256": audio_sha,
            "approved_at": self._now(),
            "updated_at": self._now(),
            "tts_locked": True,
        })
        self._write_json(job_dir / "job.json", job)
        return job

    def validated_approved_audio(self, *, user_id: int, job_id: str) -> Dict[str, Any]:
        job = self.create_or_get(user_id=user_id, job_id=job_id)
        if job.get("tts_locked") is not True or job.get("status") != "narration_approved":
            raise ProductionJobStoreError("O áudio deste trabalho ainda não foi aprovado.")
        mp3 = Path(str(job.get("approved_audio_path") or ""))
        meta_path = Path(str(job.get("approved_audio_meta_path") or ""))
        if not mp3.is_file() or mp3.stat().st_size <= 512 or not meta_path.is_file():
            raise ProductionJobStoreError("O MP3 aprovado deste trabalho não está disponível.")
        actual_sha = self._sha256_file(mp3)
        if actual_sha != str(job.get("approved_audio_sha256") or ""):
            raise ProductionJobStoreError("A integridade do MP3 aprovado não confere.")
        meta = self._read_json(meta_path)
        if meta.get("approved") is not True or str(meta.get("preview_id") or "") != str(job.get("approved_preview_id") or ""):
            raise ProductionJobStoreError("Metadados do MP3 aprovado estão inválidos.")
        return {"job": job, "audio_path": mp3, "meta": meta}


production_job_store = ProductionJobStore()
