"""Configuração de armazenamento e ambiente do V3.

Por padrão, o V3 usa diretórios próprios e nunca reutiliza /data/media do
pipeline legado. O ambiente de testes recebe outra raiz.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class PipelineRuntime:
    environment: str
    root: Path

    @classmethod
    def for_environment(cls, environment: str) -> "PipelineRuntime":
        environment = str(environment or "").strip().lower()
        if environment not in {"production", "test"}:
            raise ValueError("environment deve ser production ou test")

        configured = os.getenv("CODEXIA_PIPELINE_ROOT")
        if configured:
            root = Path(configured) / environment
        elif environment == "test":
            root = Path("/data/pipeline_test")
        else:
            root = Path("/data/pipeline_v3")

        return cls(environment=environment, root=root)

    @classmethod
    def from_env(cls) -> "PipelineRuntime":
        return cls.for_environment(os.getenv("CODEXIA_PIPELINE_ENV", "production"))

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def outputs_dir(self) -> Path:
        return self.root / "outputs"

    def ensure_directories(self) -> None:
        for directory in (self.tasks_dir, self.assets_dir, self.outputs_dir):
            directory.mkdir(parents=True, exist_ok=True)
