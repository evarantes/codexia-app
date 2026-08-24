from pathlib import Path

import pytest

from local_worker.agent import LocalRenderAgent, WorkerError


ROOT = Path(__file__).resolve().parents[1]


def test_agent_rejects_plain_http():
    with pytest.raises(WorkerError, match="HTTPS"):
        LocalRenderAgent(base_url="http://127.0.0.1:8000", token="x" * 32)


def test_agent_requires_dedicated_nontrivial_token():
    with pytest.raises(WorkerError, match="Token"):
        LocalRenderAgent(base_url="https://codexia.example", token="short")


def test_agent_has_no_database_or_redis_client_imports():
    source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8")
    forbidden = (
        "psycopg",
        "psycopg2",
        "sqlalchemy",
        "redis.Redis",
        "from app.database",
        "from app.redis_client",
    )
    for token in forbidden:
        assert token not in source


def test_agent_is_outbound_only_and_has_no_listener():
    source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8")
    forbidden = ("socket.bind(", ".listen(", "HTTPServer(", "uvicorn.run(", "Flask(")
    for token in forbidden:
        assert token not in source
    assert "requests.Session()" in source
    assert "https://" in source


def test_agent_never_calls_paid_media_or_publish_endpoints():
    source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8").lower()
    forbidden_paths = (
        "/images/generations",
        "openai.images",
        "text-to-speech",
        "elevenlabs",
        "suno",
        "/youtube/upload",
        "/publish",
    )
    for token in forbidden_paths:
        assert token not in source


def test_server_fails_closed_without_explicit_local_worker_opt_in():
    from app.routers.local_render_worker import _eligible_payload

    base = {"force_render_only": True, "force_reuse_assets": True, "auto_upload": False}
    assert _eligible_payload({"payload": base}) is None
    allowed = dict(base, local_render_worker_allowed=True)
    assert _eligible_payload({"payload": allowed}) == allowed
    assert _eligible_payload({"payload": dict(allowed, auto_upload=True)}) is None


def test_server_uses_existing_execution_lease_and_never_exposes_arbitrary_paths():
    source = (ROOT / "app" / "routers" / "local_render_worker.py").read_text(encoding="utf-8")
    assert "acquire_task_execution_lease" in source
    assert "heartbeat_task_execution_lease" in source
    assert "release_task_execution_lease" in source
    assert '"_path"' in source
    assert 'if k != "_path"' in source
    assert "asset_index" in source


def test_one_video_at_a_time_and_resource_limits_are_hard_coded():
    source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8")
    assert "self._single_job_lock = threading.Lock()" in source
    assert "self._single_job_lock.acquire(blocking=False)" in source
    assert "max_ram_percent" in source
    assert "min_free_disk_gb" in source
    assert "min(2" in source


def test_hardened_build_wires_router():
    source = (ROOT / "scripts" / "apply_ready_video_asset_repair_v3.py").read_text(encoding="utf-8")
    patcher = (ROOT / "scripts" / "apply_local_render_worker_phase1.py").read_text(encoding="utf-8")
    assert "local_worker_phase1.apply()" in source
    assert "local_worker_phase1.check()" in source
    assert "app.include_router(local_render_worker.router)" in patcher
