from app.services.pipeline_v3.contract import PipelineV3Request
from app.services.pipeline_v3.duration import parse_duration
from app.services.pipeline_v3.state import PipelineState, can_transition


def test_duration_accepts_seconds_minutes_and_clock():
    assert parse_duration("10s").seconds == 10
    assert parse_duration("1m").seconds == 60
    assert parse_duration("00:01:30").seconds == 90


def test_contract_is_idempotent_and_environment_scoped():
    first = PipelineV3Request.create(
        title="Teste curto",
        topic="Uma história de teste",
        duration="30s",
        environment="test",
    )
    second = PipelineV3Request.create(
        title="Teste curto",
        topic="Uma história de teste",
        duration="30s",
        environment="test",
    )
    production = PipelineV3Request.create(
        title="Teste curto",
        topic="Uma história de teste",
        duration="30s",
        environment="production",
    )

    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key != production.idempotency_key


def test_state_machine_does_not_skip_steps():
    assert can_transition(PipelineState.CREATED, PipelineState.PLANNING)
    assert not can_transition(PipelineState.CREATED, PipelineState.RENDERING)
    assert can_transition(PipelineState.RENDERING, PipelineState.READY)
    assert can_transition(PipelineState.FAILED, PipelineState.CREATED)
