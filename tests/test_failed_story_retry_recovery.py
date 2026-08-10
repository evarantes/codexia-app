import inspect
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.routers.youtube import (
    _dispatch_task_result,
    _load_latest_recoverable_story_video_task,
    discard_failed_task,
    retry_task,
)
from fastapi import HTTPException


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self.rows)


class FailedStoryRetryRecoveryTests(unittest.TestCase):
    def test_latest_failed_story_with_payload_is_recoverable(self):
        payload = {"mode": "story", "story_content": "Texto", "duration": 3}
        row = SimpleNamespace(
            id="task-1",
            status="failed",
            progress=78,
            message="Falha de validação",
            result_json=json.dumps({"kind": "youtube_story_video", "payload": payload}),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self.assertIs(_load_latest_recoverable_story_video_task(_FakeDb([row])), row)

    def test_dispatch_metadata_preserves_expensive_assets(self):
        prior = {
            "payload": {"mode": "story"},
            "script": {"scenes": [{"text": "Cena"}]},
            "render_report": {"audio_generation": {"output_path": "/data/audio.mp3"}},
        }
        with patch("app.routers.youtube.get_task", return_value={"result": prior}):
            merged = _dispatch_task_result("task-1", {"mode": "story", "force_reuse_assets": True}, "thread")
        self.assertEqual(merged["script"], prior["script"])
        self.assertEqual(merged["render_report"], prior["render_report"])
        self.assertTrue(merged["payload"]["force_reuse_assets"])

    def test_retry_uses_canonical_dispatch_and_not_raw_legacy_thread(self):
        source = inspect.getsource(retry_task)
        self.assertIn("_dispatch_video_generation_task(payload, task_id)", source)
        self.assertIn('"pipeline": "unified_video_pipeline"', source)
        self.assertNotIn("threading.Thread(target=process_video_generation", source)

    def test_retry_endpoint_dispatches_same_failed_task(self):
        task = {
            "status": "failed",
            "progress": 78,
            "updated_at": datetime.utcnow().isoformat(),
            "result": {
                "payload": {
                    "mode": "story",
                    "kind": "devotional",
                    "duration": 3,
                    "story_content": "Texto para recuperar",
                }
            },
        }
        fake_db = Mock()
        fake_pipeline = Mock()
        fake_pipeline.transition_status.return_value = None
        with (
            patch("app.routers.youtube.acquire_distributed_lock", return_value={"backend": "test"}),
            patch("app.routers.youtube.release_distributed_lock"),
            patch("app.routers.youtube.get_task", return_value=task),
            patch("app.routers.youtube._maybe_enable_render_only_flags", side_effect=lambda payload, _task_id: payload),
            patch("app.routers.youtube.reset_task_for_retry", return_value={"status": "processing"}),
            patch("app.routers.youtube.SessionLocal", return_value=fake_db),
            patch("app.routers.youtube.unified_video_pipeline", return_value=fake_pipeline),
            patch("app.routers.youtube._dispatch_video_generation_task") as dispatch,
        ):
            result = retry_task("task-1", _admin=SimpleNamespace(id=1))
        self.assertEqual(result["task_id"], "task-1")
        self.assertTrue(result["reused_task"])
        dispatched_payload = dispatch.call_args.args[0]
        self.assertTrue(dispatched_payload["force_reuse_assets"])
        dispatch.assert_called_once_with(dispatched_payload, "task-1")

    def test_frontend_keeps_failed_task_link_and_blocks_duplicate_generate(self):
        html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        poll_story = html.split("async pollStoryTask(taskId)", 1)[1].split("async generateStoryShorts", 1)[0]
        failed_branch = poll_story.split("if (status === 'failed') {", 1)[1].split("if (status === 'cancelled')", 1)[0]
        self.assertIn("localStorage.setItem('ytStoryTaskId'", failed_branch)
        self.assertIn("this.ytStoryTaskId = String(taskId)", failed_branch)
        self.assertNotIn("localStorage.removeItem('ytStoryTaskId')", failed_branch)
        self.assertNotIn("alert(data.message", failed_branch)
        self.assertIn("Esta solicitação pode ser recuperada", html)
        self.assertIn("Reiniciar agora", html)
        self.assertIn("async retryStoryTaskFromQueue(item)", html)
        self.assertIn("async retryLatestRecoverableStoryTask()", html)
        self.assertNotIn("ytStoryRetryLoading || !ytStoryTaskId || !ytStoryTask", html)
        self.assertIn("message: retryError", html)

    def test_discard_failed_task_cancels_only_selected_task(self):
        task = {"status": "failed", "progress": 20, "result": {"payload": {"mode": "story"}}}
        fake_db = Mock()
        fake_pipeline = Mock()
        fake_pipeline.transition_status.return_value = None
        with (
            patch("app.routers.youtube.get_task", return_value=task),
            patch("app.routers.youtube.request_cancel_task", return_value={"status": "cancelled"}) as cancel_one,
            patch("app.routers.youtube.SessionLocal", return_value=fake_db),
            patch("app.routers.youtube._unified_enabled", return_value=True),
            patch("app.routers.youtube.unified_video_pipeline", return_value=fake_pipeline),
            patch("app.routers.youtube._kick_story_video_task_queue_async"),
        ):
            result = discard_failed_task("task-old", _admin=SimpleNamespace(id=1))
        self.assertTrue(result["discarded"])
        self.assertEqual(result["status"], "cancelled")
        cancel_one.assert_called_once_with("task-old", message="Tarefa falhada descartada pelo usuário.")
        fake_pipeline.transition_status.assert_called_once()

    def test_discard_rejects_task_that_is_still_processing(self):
        with patch("app.routers.youtube.get_task", return_value={"status": "processing", "progress": 20}):
            with self.assertRaises(HTTPException) as ctx:
                discard_failed_task("task-live", _admin=SimpleNamespace(id=1))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_frontend_exposes_individual_discard_without_global_cancel(self):
        html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Descartar tarefa", html)
        self.assertIn("async discardStoryTask(taskIdOverride = null)", html)
        self.assertIn("/discard`, { method: 'POST' }", html)
        self.assertIn("this.ytStoryTaskId = null", html)


if __name__ == "__main__":
    unittest.main()
