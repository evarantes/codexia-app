"""Armazenamento atômico dos manifestos do Pipeline V3.

O V3 persiste o contrato e os checkpoints em arquivos próprios. Isso evita
qualquer dependência das tabelas video_tasks/unified_videos do pipeline legado.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import UUID, uuid4

from .runtime import PipelineRuntime


class ManifestNotFound(FileNotFoundError):
    """Raised when a V3 task manifest does not exist."""


class PipelineV3ManifestStore:
    def __init__(self, runtime: PipelineRuntime | None = None) -> None:
        self.runtime = runtime or PipelineRuntime.from_env()
        self.runtime.ensure_directories()

    def _task_dir(self, task_id: str | UUID) -> Path:
        return self.runtime.tasks_dir / str(task_id)

    def _manifest_path(self, task_id: str | UUID) -> Path:
        return self._task_dir(task_id) / "manifest.json"

    def create(self, manifest: dict[str, Any]) -> dict[str, Any]:
        task_id = str(manifest.get("task_id") or uuid4())
        path = self._manifest_path(task_id)
        if path.exists():
            raise FileExistsError(f"manifesto V3 já existe: {task_id}")
        payload = dict(manifest)
        payload["task_id"] = task_id
        self._write(path, payload, overwrite=False)
        return payload

    def get(self, task_id: str | UUID) -> dict[str, Any]:
        path = self._manifest_path(task_id)
        if not path.exists():
            raise ManifestNotFound(str(task_id))
        return json.loads(path.read_text(encoding="utf-8"))

    def update(self, task_id: str | UUID, **changes: Any) -> dict[str, Any]:
        current = self.get(task_id)
        current.update(changes)
        self._write(self._manifest_path(task_id), current, overwrite=True)
        return current

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        for path in self.runtime.tasks_dir.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("idempotency_key") == key:
                return manifest
        return None

    @staticmethod
    def _write(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise FileExistsError(str(path))
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
