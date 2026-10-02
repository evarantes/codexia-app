from pathlib import Path

import pytest

from app.services.pipeline_v3.contract import PipelineV3Request
from app.services.pipeline_v3.manifest_store import PipelineV3ManifestStore
from app.services.pipeline_v3.orchestrator import PipelineV3Orchestrator
from app.services.pipeline_v3.runtime import PipelineRuntime
from app.services.pipeline_v3.state import PipelineState


def make_orchestrator(tmp_path: Path) -> PipelineV3Orchestrator:
    runtime = PipelineRuntime(environment="test", root=tmp_path)
    store = PipelineV3ManifestStore(runtime)
    return PipelineV3Orchestrator(runtime, store)


def test_submit_persists_manifest_without_database(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    request = PipelineV3Request.create(
        title="Teste curto",
        topic="Uma história de 30 segundos",
        duration="30s",
        environment="test",
    )

    first = orchestrator.submit(request)
    second = orchestrator.submit(request)

    assert first["task_id"] == second["task_id"]
    assert first["duration_seconds"] == 30.0
    assert (tmp_path / "tasks" / first["task_id"] / "manifest.json").exists()


def test_transition_is_sequential_and_checkpointed(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    request = PipelineV3Request.create(
        title="Teste de estados",
        topic="Fluxo",
        duration="1m",
        environment="test",
    )
    task = orchestrator.submit(request)

    planned = orchestrator.transition(task["task_id"], PipelineState.PLANNING)
    assert planned["progress"] == 10

    saved = orchestrator.transition(
        task["task_id"],
        PipelineState.SCRIPT_SAVED,
        checkpoint={"script_path": "script.txt"},
    )
    assert saved["checkpoints"]["script_saved"]["script_path"] == "script.txt"

    with pytest.raises(ValueError, match="transição V3 inválida"):
        orchestrator.transition(task["task_id"], PipelineState.READY)


def test_production_manifest_is_separate_from_test(tmp_path: Path) -> None:
    test_runtime = PipelineRuntime(environment="test", root=tmp_path / "test")
    production_runtime = PipelineRuntime(environment="production", root=tmp_path / "production")
    test_store = PipelineV3ManifestStore(test_runtime)
    production_store = PipelineV3ManifestStore(production_runtime)

    assert test_store.runtime.tasks_dir != production_store.runtime.tasks_dir
