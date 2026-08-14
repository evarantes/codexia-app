from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from app.routers import youtube


class _WorkerRow:
    def __init__(self, heartbeat):
        self.last_heartbeat = heartbeat


class CX33RetryDispatchRegressionTests(unittest.TestCase):
    def test_rq_worker_online_uses_rq_registration_even_with_idle_heartbeat(self):
        class FakeWorker:
            @classmethod
            def count(cls, *args, **kwargs):
                return 1

            @classmethod
            def all(cls, *args, **kwargs):
                return [_WorkerRow(datetime.now(timezone.utc))]

        with ExitStack() as stack:
            stack.enter_context(patch.object(youtube, "conn", object()))
            stack.enter_context(patch.object(youtube, "RQ_AVAILABLE", True))
            stack.enter_context(patch.object(youtube, "Worker", FakeWorker))
            self.assertTrue(youtube._rq_workers_online())

    def test_rq_worker_offline_when_rq_has_no_registered_workers(self):
        class FakeWorker:
            @classmethod
            def count(cls, *args, **kwargs):
                return 0

        with ExitStack() as stack:
            stack.enter_context(patch.object(youtube, "conn", object()))
            stack.enter_context(patch.object(youtube, "RQ_AVAILABLE", True))
            stack.enter_context(patch.object(youtube, "Worker", FakeWorker))
            self.assertFalse(youtube._rq_workers_online())

    def test_production_never_falls_back_to_local_when_worker_offline(self):
        updates = []

        class ForbiddenThread:
            def __init__(self, *args, **kwargs):
                raise AssertionError("produção local não pode iniciar no app principal")

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"APP_ENV": "production"}, clear=False))
            stack.enter_context(patch.object(youtube, "conn", object()))
            stack.enter_context(patch.object(youtube, "_rq_workers_online", lambda: False))
            stack.enter_context(patch.object(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True}))
            stack.enter_context(patch.object(youtube, "_requires_isolated_video_process", lambda payload, task_id: False))
            stack.enter_context(patch.object(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload))
            stack.enter_context(patch.object(youtube, "get_task", lambda task_id: {"progress": 57}))
            stack.enter_context(patch.object(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs)))
            stack.enter_context(patch.object(youtube.threading, "Thread", ForbiddenThread))

            youtube._dispatch_video_generation_task({"duration": 3}, "task-1")

        self.assertTrue(updates)
        self.assertEqual(updates[-1]["status"], "pending")
        self.assertEqual(updates[-1]["progress"], 57)
        self.assertIn("NÃO será", updates[-1]["message"])

    def test_production_enqueues_when_cx33_worker_is_online(self):
        enqueued = []
        updates = []

        class Queue:
            def enqueue(self, *args, **kwargs):
                enqueued.append((args, kwargs))

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"APP_ENV": "production"}, clear=False))
            stack.enter_context(patch.object(youtube, "conn", object()))
            stack.enter_context(patch.object(youtube, "rq_queue", Queue()))
            stack.enter_context(patch.object(youtube, "_rq_workers_online", lambda: True))
            stack.enter_context(patch.object(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True}))
            stack.enter_context(patch.object(youtube, "_requires_isolated_video_process", lambda payload, task_id: False))
            stack.enter_context(patch.object(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload))
            stack.enter_context(patch.object(youtube, "get_task", lambda task_id: {"progress": 20}))
            stack.enter_context(patch.object(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs)))

            youtube._dispatch_video_generation_task({"duration": 3}, "task-2")

        self.assertEqual(len(enqueued), 1)
        self.assertEqual(updates[-1]["status"], "processing")
        self.assertEqual(updates[-1]["progress"], 20)
        self.assertIn("CX33", updates[-1]["message"])

    def test_diagnostic_panel_renders_checks_and_task_message(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("ytStoryAssistReport.checks", html)
        self.assertIn("ytStoryAssistReport.task.message", html)
        self.assertIn("Status real:", html)


if __name__ == "__main__":
    unittest.main()
