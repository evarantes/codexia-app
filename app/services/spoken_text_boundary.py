"""Compatibilidade para imports antigos da fronteira falável.

Não existe parser independente neste módulo. Toda decisão sobre o que pode ser
falado pertence exclusivamente a ``app.services.narration_core``.
"""
from __future__ import annotations

from typing import Any

from app.services.narration_core import (
    NARRATION_CORE_VERSION,
    build_narration_artifact,
)


SPOKEN_TEXT_BOUNDARY_VERSION = NARRATION_CORE_VERSION


def prepare_spoken_narration_text(text: Any) -> str:
    return build_narration_artifact(text).spoken_text


__all__ = ["SPOKEN_TEXT_BOUNDARY_VERSION", "prepare_spoken_narration_text"]
