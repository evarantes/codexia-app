from types import SimpleNamespace

import app.services.worker_health_server as health


class FakeRedis:
    def ping(self):
        return True


class FakeWorker:
    name = "worker-test"

    def get_state(self):
        return "idle"


def test_worker_health_requires_all_dependencies(monkeypatch):
    class FakeConnection:
        def execute(self, _query):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(health, "engine", SimpleNamespace(connect=lambda: FakeConnection()))
    snapshot = health.build_health_snapshot(FakeWorker(), FakeRedis())
    assert snapshot["status"] == "ok"
    assert all(check["ok"] for check in snapshot["checks"].values())


def test_worker_health_is_error_when_rq_is_not_registered(monkeypatch):
    class MissingWorker(FakeWorker):
        def get_state(self):
            raise RuntimeError("missing")

    class FakeConnection:
        def execute(self, _query):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(health, "engine", SimpleNamespace(connect=lambda: FakeConnection()))
    snapshot = health.build_health_snapshot(MissingWorker(), FakeRedis())
    assert snapshot["status"] == "error"
    assert snapshot["checks"]["rq"]["ok"] is False
