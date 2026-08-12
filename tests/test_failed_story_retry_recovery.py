import inspect
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.routers.youtube as youtube_router
from app.routers.youtube import (
    _apply_youtube_auto_editorial_intelligence,
    _dispatch_task_result,
    _load_latest_recoverable_story_video_task,
    cancel_all_tasks,
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


class _CancelAllDb:
    def __init__(self, tasks, scheduled=None):
        self.tasks = tasks
        self.scheduled = scheduled or []

    def query(self, model):
        if getattr(model, "__name__", "") == "VideoTask":
            return _FakeQuery(self.tasks)
        return _FakeQuery(self.scheduled)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def eval(self, _script, _numkeys, key, token):
        current = self.values.get(key)
        if str(current or "") != str(token or ""):
            return 0
        return self.delete(key)


class FailedStoryRetryRecoveryTests(unittest.TestCase):
    def test_disabled_editorial_review_preserves_complete_plan(self):
        original_plan = {
            "title": "A fé que vence o medo",
            "description": "Descrição original",
            "scenes": [
                {
                    "text": "Narração da primeira cena.",
                    "image_prompt": "Biblical cinematic scene",
                }
            ],
        }
        editorial_only = {
            "editorial_intelligence": {
                "status": "disabled",
                "summary": "Editor desabilitado por configuração.",
            }
        }
        helper = Mock()
        helper.review_plan.return_value = {
            "status": "disabled",
            "plan_updates": editorial_only,
        }

        with (
            patch("app.routers.youtube.get_latest_settings", return_value=SimpleNamespace()),
            patch("app.routers.youtube.serialize_official_factory_settings", return_value={}),
            patch("app.routers.youtube.EditorialIntelligenceService", return_value=helper),
        ):
            merged = _apply_youtube_auto_editorial_intelligence(
                None,
                original_plan,
                ai_service=Mock(),
                task_id="task-editorial-disabled",
            )

        self.assertEqual(merged["title"], original_plan["title"])
        self.assertEqual(merged["description"], original_plan["description"])
        self.assertEqual(merged["scenes"], original_plan["scenes"])
        self.assertEqual(merged["editorial_intelligence"]["status"], "disabled")
        self.assertNotIn("editorial_intelligence", original_plan)

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

    def test_frontend_keeps_failed_task_in_session_without_auto_opening_it_after_refresh(self):
        html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        poll_story = html.split("async pollStoryTask(taskId)", 1)[1].split("async generateStoryShorts", 1)[0]
        failed_branch = poll_story.split("if (status === 'failed') {", 1)[1].split("if (status === 'cancelled')", 1)[0]
        self.assertIn("this.ytStoryTaskId = String(taskId)", failed_branch)
        self.assertIn("localStorage.removeItem('ytStoryTaskId')", failed_branch)
        queue_loader = html.split("async fetchActiveVideoTasks", 1)[1].split("openStoryTaskFromQueue(item)", 1)[0]
        self.assertIn("!t.recoverable", queue_loader)
        self.assertNotIn("|| items.find(t => t && t.can_open && t.task_id)\n", queue_loader)
        self.assertNotIn("alert(data.message", failed_branch)
        self.assertIn("Esta solicitação pode ser recuperada", html)
        self.assertIn("Reiniciar agora", html)
        self.assertIn("async retryStoryTaskFromQueue(item)", html)
        self.assertIn("async retryLatestRecoverableStoryTask()", html)
        self.assertNotIn("ytStoryRetryLoading || !ytStoryTaskId || !ytStoryTask", html)
        self.assertIn("message: retryError", html)

    def test_recoverable_queue_item_is_explicitly_marked_as_old_and_never_auto_opened(self):
        source = inspect.getsource(youtube_router.list_story_video_task_queue)
        self.assertIn('"Histórico recuperável — não é uma nova produção"', source)
        self.assertIn('"Falha antiga — abra ou reinicie somente se desejar"', source)
        self.assertIn('"auto_open": False', source)

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

    def test_global_stop_cancels_processing_and_failed_story_tasks_through_task_manager(self):
        processing = SimpleNamespace(
            id="task-processing",
            status="processing",
            result_json=json.dumps({"kind": "youtube_story_video", "payload": {"mode": "story"}}),
        )
        failed = SimpleNamespace(
            id="task-failed",
            status="failed",
            result_json=json.dumps({"kind": "youtube_story_video", "payload": {"mode": "story"}}),
        )
        task_db = _CancelAllDb([processing, failed])
        series_db = _CancelAllDb([])
        fake_series_service = Mock()
        fake_series_service.pause_for_server_shutdown.return_value = {
            "paused_series": 1,
            "cancelled_series_episodes": 1,
        }

        with (
            patch("app.routers.youtube.conn", None),
            patch("app.routers.youtube.SessionLocal", side_effect=[task_db, series_db]),
            patch("app.routers.youtube.request_cancel_task", return_value={"status": "cancelled"}) as cancel,
            patch("app.services.youtube_series_service.youtube_series_service", fake_series_service),
            patch("app.routers.youtube._kick_story_video_task_queue_async"),
        ):
            result = cancel_all_tasks(_admin=SimpleNamespace(id=1))

        self.assertEqual(result["cancelled_tasks"], 2)
        self.assertEqual(result["paused_series"], 1)
        self.assertEqual(
            {call_item.args[0] for call_item in cancel.call_args_list},
            {"task-processing", "task-failed"},
        )
        fake_series_service.pause_for_server_shutdown.assert_called_once_with(
            series_db,
            cancelled_task_ids=["task-failed", "task-processing"],
        )

    def test_global_stop_releases_redis_barrier_before_next_request(self):
        redis = _FakeRedis()
        task_db = _CancelAllDb([])
        series_db = _CancelAllDb([])
        fake_series_service = Mock()
        fake_series_service.pause_for_server_shutdown.return_value = {
            "paused_series": 0,
            "cancelled_series_episodes": 0,
        }

        with (
            patch("app.routers.youtube.conn", redis),
            patch("app.routers.youtube.SessionLocal", side_effect=[task_db, series_db]),
            patch("app.services.youtube_series_service.youtube_series_service", fake_series_service),
            patch("app.routers.youtube._kick_story_video_task_queue_async"),
        ):
            result = cancel_all_tasks(_admin=SimpleNamespace(id=1))
            self.assertFalse(youtube_router._cancel_all_active())

        self.assertEqual(result["status"], "ok")
        self.assertNotIn(youtube_router._CANCEL_ALL_KEY, redis.values)

    def test_global_stop_releases_redis_barrier_even_when_snapshot_fails(self):
        redis = _FakeRedis()
        with (
            patch("app.routers.youtube.conn", redis),
            patch("app.routers.youtube._cancel_all_tasks_snapshot", side_effect=RuntimeError("db unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "db unavailable"):
                cancel_all_tasks(_admin=SimpleNamespace(id=1))
            self.assertFalse(youtube_router._cancel_all_active())

        self.assertNotIn(youtube_router._CANCEL_ALL_KEY, redis.values)

    def test_parallel_global_stop_cannot_replace_or_release_owner_barrier(self):
        redis = _FakeRedis()
        redis.values[youtube_router._CANCEL_ALL_KEY] = "owner-token"
        with (
            patch("app.routers.youtube.conn", redis),
            patch("app.routers.youtube._cancel_all_tasks_snapshot") as snapshot,
        ):
            with self.assertRaises(HTTPException) as ctx:
                cancel_all_tasks(_admin=SimpleNamespace(id=1))

        self.assertEqual(ctx.exception.status_code, 409)
        snapshot.assert_not_called()
        self.assertEqual(redis.values[youtube_router._CANCEL_ALL_KEY], "owner-token")

    def test_old_owner_cannot_release_a_newer_global_stop_barrier(self):
        redis = _FakeRedis()
        redis.values[youtube_router._CANCEL_ALL_KEY] = "new-owner-token"
        with patch("app.routers.youtube.conn", redis):
            youtube_router._release_cancel_all_barrier("old-owner-token")

        self.assertEqual(redis.values[youtube_router._CANCEL_ALL_KEY], "new-owner-token")

    def test_frontend_exposes_individual_discard_without_global_cancel(self):
        html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Descartar tarefa", html)
        self.assertIn("async discardStoryTask(taskIdOverride = null)", html)
        self.assertIn("/discard`, { method: 'POST' }", html)
        self.assertIn("this.ytStoryTaskId = null", html)
        self.assertIn("autoOpen = true", html)
        self.assertIn("autoOpen: false", html)
        poll_story = html.split("async pollStoryTask(taskId)", 1)[1].split("async generateStoryShorts", 1)[0]
        self.assertGreaterEqual(
            poll_story.count("String(this.ytStoryTaskId || '') !== String(taskId || '')"),
            2,
        )


if __name__ == "__main__":
    unittest.main()
