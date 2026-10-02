"""Orquestração inicial e determinística do Pipeline V3."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .contract import PipelineV3Request
from .manifest_store import PipelineV3ManifestStore
from .runtime import PipelineRuntime
from .state import PipelineState, can_transition


class PipelineV3Orchestrator:
    """Cria e conduz contratos V3 sem tocar no pipeline legado."""

    def __init__(
        self,
        runtime: PipelineRuntime | None = None,
        store: PipelineV3ManifestStore | None = None,
    ) -> None:
        self.runtime = runtime or PipelineRuntime.from_env()
        self.store = store or PipelineV3ManifestStore(self.runtime)

    def submit(self, request: PipelineV3Request) -> dict[str, Any]:
        if request.environment != self.runtime.environment:
            raise ValueError(
                f"request environment={request.environment!r} não corresponde "
                f"ao runtime={self.runtime.environment!r}"
            )

        existing = self.store.find_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            return existing

        now = datetime.now(timezone.utc).isoformat()
        return self.store.create(
            {
                "idempotency_key": request.idempotency_key,
                "environment": request.environment,
                "title": request.title,
                "topic": request.topic,
                "duration_seconds": request.duration.seconds,
                "channel_id": request.channel_id,
                "options": request.options or {},
                "state": PipelineState.CREATED.value,
                "progress": 0,
                "checkpoints": {},
                "created_at": now,
                "updated_at": now,
            }
        )

    def status(self, task_id: str) -> dict[str, Any]:
        return self.store.get(task_id)

    def transition(
        self,
        task_id: str,
        target: PipelineState | str,
        *,
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest = self.store.get(task_id)
        current = PipelineState(manifest["state"])
        target_state = PipelineState(target)
        if not can_transition(current, target_state):
            raise ValueError(f"transição V3 inválida: {current.value} -> {target_state.value}")

        checkpoints = dict(manifest.get("checkpoints") or {})
        if checkpoint is not None:
            checkpoints[target_state.value] = checkpoint

        progress = {
            PipelineState.CREATED: 0,
            PipelineState.PLANNING: 10,
            PipelineState.SCRIPT_SAVED: 25,
            PipelineState.AUDIO_READY: 45,
            PipelineState.VISUALS_READY: 65,
            PipelineState.CAPTIONS_READY: 75,
            PipelineState.RENDERING: 90,
            PipelineState.READY: 100,
        }.get(target_state, manifest.get("progress", 0))

        return self.store.update(
            task_id,
            state=target_state.value,
            progress=progress,
            checkpoints=checkpoints,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
