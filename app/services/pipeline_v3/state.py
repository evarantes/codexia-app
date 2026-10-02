"""Máquina de estados independente do pipeline legado."""
from __future__ import annotations

from enum import StrEnum


class PipelineState(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    SCRIPT_SAVED = "script_saved"
    AUDIO_READY = "audio_ready"
    VISUALS_READY = "visuals_ready"
    CAPTIONS_READY = "captions_ready"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ORDER = {
    PipelineState.CREATED: 0,
    PipelineState.PLANNING: 1,
    PipelineState.SCRIPT_SAVED: 2,
    PipelineState.AUDIO_READY: 3,
    PipelineState.VISUALS_READY: 4,
    PipelineState.CAPTIONS_READY: 5,
    PipelineState.RENDERING: 6,
    PipelineState.READY: 7,
}


def can_transition(current: PipelineState, target: PipelineState) -> bool:
    if current == target:
        return True
    if current in (PipelineState.FAILED, PipelineState.CANCELLED):
        return target == PipelineState.CREATED
    if target in (PipelineState.FAILED, PipelineState.CANCELLED):
        return current != PipelineState.READY
    if current not in _ORDER or target not in _ORDER:
        return False
    return _ORDER[target] == _ORDER[current] + 1
