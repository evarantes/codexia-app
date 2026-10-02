"""API controlada para o runtime isolado do Pipeline V3.

A rota fica desativada por padrão. Ela apenas cria e consulta contratos V3 nesta
etapa; o executor de mídia será conectado depois que o contrato persistente
passar pelos testes.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.pipeline_v3.contract import PipelineV3Request
from app.services.pipeline_v3.manifest_store import ManifestNotFound
from app.services.pipeline_v3.orchestrator import PipelineV3Orchestrator
from app.services.pipeline_v3.runtime import PipelineRuntime
from app.services.pipeline_v3.state import PipelineState


router = APIRouter(prefix="/pipeline-v3", tags=["pipeline-v3"])


class PipelineV3SubmitBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    topic: str = Field(min_length=1, max_length=5000)
    duration: int | float | str = Field(description="segundos, 1m, MM:SS ou HH:MM:SS")
    environment: str = Field(default="test", pattern="^(test|production)$")
    channel_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class PipelineV3TransitionBody(BaseModel):
    target: PipelineState
    checkpoint: dict[str, Any] | None = None


def _enabled(environment: str) -> bool:
    if environment == "test":
        return os.getenv("CODEXIA_V3_TEST_ENABLED", "false").lower() in {"1", "true", "yes"}
    return os.getenv("CODEXIA_V3_ENABLED", "false").lower() in {"1", "true", "yes"}


def _orchestrator(environment: str) -> PipelineV3Orchestrator:
    os.environ["CODEXIA_PIPELINE_ENV"] = environment
    return PipelineV3Orchestrator(PipelineRuntime.from_env())


@router.post("/tasks", status_code=201)
def submit_task(body: PipelineV3SubmitBody) -> dict[str, Any]:
    if not _enabled(body.environment):
        raise HTTPException(status_code=404, detail="Pipeline V3 ainda não está ativado neste ambiente")
    try:
        request = PipelineV3Request.create(
            title=body.title,
            topic=body.topic,
            duration=body.duration,
            environment=body.environment,
            channel_id=body.channel_id,
            options=body.options,
        )
        return _orchestrator(body.environment).submit(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    for environment in ("test", "production"):
        if not _enabled(environment):
            continue
        try:
            return _orchestrator(environment).status(task_id)
        except ManifestNotFound:
            continue
    raise HTTPException(status_code=404, detail="tarefa V3 não encontrada")


@router.post("/tasks/{task_id}/transition")
def transition_task(task_id: str, body: PipelineV3TransitionBody) -> dict[str, Any]:
    for environment in ("test", "production"):
        if not _enabled(environment):
            continue
        try:
            return _orchestrator(environment).transition(
                task_id,
                body.target,
                checkpoint=body.checkpoint,
            )
        except ManifestNotFound:
            continue
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="tarefa V3 não encontrada")
