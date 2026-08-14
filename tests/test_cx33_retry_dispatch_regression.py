from datetime import datetime, timezone

import pytest

from app.routers import youtube


class _WorkerRow:
    def __init__(self, heartbeat):
        self.last_heartbeat = heartbeat


def test_rq_worker_online_accepts_timezone_aware_heartbeat(monkeypatch):
    class FakeWorker:
        @classmethod
        def all(cls, *args, **kwargs):
            return [_WorkerRow(datetime.now(timezone.utc))]

    monkeypatch.setattr(youtube, "conn", object())
    monkeypatch.setattr(youtube, "RQ_AVAILABLE", True)
    monkeypatch.setattr(youtube, "Worker", FakeWorker)

    assert youtube._rq_workers_online() is True


def test_production_never_falls_back_to_local_when_worker_offline(monkeypatch):
    updates = []
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(youtube, "conn", object())
    monkeypatch.setattr(youtube, "_rq_workers_online", lambda: False)
    monkeypatch.setattr(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True})
    monkeypatch.setattr(youtube, "_requires_isolated_video_process", lambda payload, task_id: False)
    monkeypatch.setattr(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload)
    monkeypatch.setattr(youtube, "get_task", lambda task_id: {"progress": 57})
    monkeypatch.setattr(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs))

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("produção local não pode iniciar no app principal")

    monkeypatch.setattr(youtube.threading, "Thread", ForbiddenThread)
    youtube._dispatch_video_generation_task({"duration": 3}, "task-1")

    assert updates
    assert updates[-1]["status"] == "pending"
    assert updates[-1]["progress"] == 57
    assert "NÃO será" in updates[-1]["message"]


def test_production_enqueues_when_cx33_worker_is_online(monkeypatch):
    enqueued = []
    updates = []

    class Queue:
        def enqueue(self, *args, **kwargs):
            enqueued.append((args, kwargs))

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(youtube, "conn", object())
    monkeypatch.setattr(youtube, "rq_queue", Queue())
    monkeypatch.setattr(youtube, "_rq_workers_online", lambda: True)
    monkeypatch.setattr(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True})
    monkeypatch.setattr(youtube, "_requires_isolated_video_process", lambda payload, task_id: False)
    monkeypatch.setattr(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload)
    monkeypatch.setattr(youtube, "get_task", lambda task_id: {"progress": 20})
    monkeypatch.setattr(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs))

    youtube._dispatch_video_generation_task({"duration": 3}, "task-2")

    assert len(enqueued) == 1
    assert updates[-1]["status"] == "processing"
    assert updates[-1]["progress"] == 20
    assert "CX33" in updates[-1]["message"]


def test_diagnostic_panel_renders_checks_and_task_message():
    html = (youtube.Path("app/static/index.html")).read_text(encoding="utf-8")
    assert "ytStoryAssistReport.checks" in html
    assert "ytStoryAssistReport.task.message" in html
    assert "Status real:" in html
