"""Fundação isolada do Pipeline V3.

Este pacote não é importado pelo pipeline legado. A ativação ocorre somente
quando a aplicação for explicitamente configurada para o V3.
"""
from .contract import PipelineV3Request
from .duration import DurationSpec, parse_duration
from .runtime import PipelineRuntime
from .state import PipelineState, can_transition

__all__ = [
    "DurationSpec",
    "PipelineRuntime",
    "PipelineState",
    "PipelineV3Request",
    "can_transition",
    "parse_duration",
]
