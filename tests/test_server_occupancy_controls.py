import inspect
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


os.environ.setdefault("ENABLE_SQLITE_DEV", "true")
os.environ.setdefault("APP_ENV", "development")
if os.environ.get("DATABASE_URL", "").startswith("postgresql://"):
    del os.environ["DATABASE_URL"]

from app.routers import youtube


class ServerOccupancyApiTests(unittest.TestCase):
    def test_processing_task_exposes_pause_and_runtime_details(self):
        now = datetime.utcnow()
        row = SimpleNamespace(
            id="task-running",
            status="processing",
            progress=48,
            message="3/8 Preparando imagens e cenas...",
            result_json=json.dumps(
                {
                    "kind": "youtube_story_video",
                    "title_hint": "Vídeo em andamento",
                    "payload": {"mode": "story", "duration": 10},
                    "pipeline_stage": "stage_3_images",
                }
            ),
            created_at=now,
            updated_at=now,
        )
        runtime = {
            "state": "working",
            "label": "Processo ativo",
            "stage": "stage_3_images",
            "last_signal_seconds": 4,
            "resources": {"available_memory_mb": 1800.0},
        }

        with patch.object(youtube, "_runtime_view_for_task", return_value=runtime):
            item = youtube._story_video_task_item_from_row(row, 1)

        self.assertTrue(item["is_current"])
        self.assertTrue(item["can_pause"])
        self.assertTrue(item["can_cancel"])
        self.assertFalse(item["can_resume"])
        self.assertEqual(item["stage"], "stage_3_images")
        self.assertEqual(item["last_signal_seconds"], 4)
        self.assertEqual(item["runtime"]["resources"]["available_memory_mb"], 1800.0)

    def test_paused_task_is_resumable_and_does_not_occupy_server(self):
        now = datetime.utcnow()
        row = SimpleNamespace(
            id="task-paused",
            status="paused",
            progress=48,
            message="Produção pausada com segurança.",
            result_json=json.dumps(
                {
                    "kind": "youtube_story_video",
                    "title_hint": "Vídeo pausado",
                    "payload": {"mode": "story", "duration": 10},
                }
            ),
            created_at=now,
            updated_at=now,
        )

        with patch.object(youtube, "_runtime_view_for_task", return_value={"state": "paused"}):
            item = youtube._story_video_task_item_from_row(row, 2)

        self.assertFalse(item["is_current"])
        self.assertFalse(item["can_pause"])
        self.assertTrue(item["can_resume"])
        self.assertEqual(item["queue_label"], "Pausada")

    def test_pending_task_can_be_paused_before_it_spends_resources(self):
        now = datetime.utcnow()
        row = SimpleNamespace(
            id="task-waiting",
            status="pending",
            progress=0,
            message="Aguardando vez na fila.",
            result_json=json.dumps({
                "kind": "youtube_story_video",
                "payload": {"mode": "story", "topic": "Próximo vídeo"},
            }),
            created_at=now,
            updated_at=now,
        )

        with patch.object(youtube, "_runtime_view_for_task", return_value={"state": "queued"}):
            item = youtube._story_video_task_item_from_row(row, 2)

        self.assertFalse(item["is_current"])
        self.assertTrue(item["can_pause"])
        self.assertTrue(item["can_cancel"])
        self.assertEqual(item["queue_label"], "Na fila")

    def test_primary_queue_item_exposes_waiting_pause_cancel_and_resume_states(self):
        now = datetime.utcnow()
        pending_job = SimpleNamespace(
            id=91,
            video_id=14,
            status="pending",
            progress=0,
            step="script",
            logs="",
            created_at=now,
            updated_at=now,
        )
        video = SimpleNamespace(
            id=14,
            title="Vídeo da série",
            status="QUEUED",
            duration_sec=600,
            created_at=now,
        )
        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = [pending_job]
        db = MagicMock()
        db.query.return_value = query

        waiting = youtube._production_video_queue_item(db, video, 1)
        self.assertEqual(waiting["status"], "pending")
        self.assertTrue(waiting["can_pause"])
        self.assertTrue(waiting["can_cancel"])
        self.assertFalse(waiting["can_resume"])

        video.status = "PAUSED"
        pending_job.status = "paused"
        paused = youtube._production_video_queue_item(db, video, 1)
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(paused["can_pause"])
        self.assertTrue(paused["can_resume"])

    def test_resume_paused_task_requeues_without_direct_dispatch(self):
        pipeline = MagicMock()
        now_iso = datetime.utcnow().isoformat()
        with (
            patch.object(youtube, "acquire_distributed_lock", return_value={}),
            patch.object(youtube, "release_distributed_lock"),
            patch.object(youtube, "get_task", return_value={
                "status": "paused",
                "progress": 48,
                "result": {"payload": {"mode": "story", "topic": "Retomar"}},
                "updated_at": now_iso,
            }),
            patch.object(youtube, "_maybe_enable_render_only_flags", side_effect=lambda payload, _task_id: payload),
            patch.object(youtube, "merge_task_result") as merge_result,
            patch.object(
                youtube,
                "enqueue_paused_task_for_resume",
                return_value={"status": "pending", "progress": 48},
            ) as enqueue_resume,
            patch.object(youtube, "unified_video_pipeline", return_value=pipeline),
            patch.object(youtube, "_kick_story_video_task_queue_async") as kick,
            patch.object(youtube, "_dispatch_video_generation_task") as dispatch,
        ):
            result = youtube.retry_task("task-paused", _admin=SimpleNamespace(id=1))

        self.assertTrue(now_iso)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["queued"])
        merge_result.assert_called_once()
        enqueue_resume.assert_called_once()
        kick.assert_called_once()
        dispatch.assert_not_called()

    def test_paused_item_does_not_block_handoff_to_next_queue(self):
        paused_row = SimpleNamespace(id="paused-only", status="paused")
        db = MagicMock()
        with (
            patch.object(youtube, "SessionLocal", return_value=db),
            patch.object(youtube, "_load_story_video_task_rows", return_value=[paused_row]),
            patch.object(youtube, "_cleanup_story_video_task_queue", return_value={"changed": False}),
            patch.object(youtube, "_is_video_factory_busy", return_value=False),
            patch.object(youtube, "_kick_primary_production_queue_async") as kick_primary,
            patch.object(youtube, "_dispatch_video_generation_task") as dispatch,
        ):
            result = youtube._kick_story_video_task_queue()

        self.assertIsNone(result)
        kick_primary.assert_called_once()
        dispatch.assert_not_called()
        db.close.assert_called_once()

    def test_busy_factory_never_starts_second_executor(self):
        pending_row = SimpleNamespace(id="pending-next", status="pending")
        db = MagicMock()
        with (
            patch.object(youtube, "SessionLocal", return_value=db),
            patch.object(youtube, "_load_story_video_task_rows", return_value=[pending_row]),
            patch.object(youtube, "_cleanup_story_video_task_queue", return_value={"changed": False}),
            patch.object(youtube, "_is_video_factory_busy", return_value=True),
            patch.object(youtube, "_dispatch_video_generation_task") as dispatch,
        ):
            result = youtube._kick_story_video_task_queue()

        self.assertIsNone(result)
        dispatch.assert_not_called()

    def test_live_executor_receives_cooperative_pause_request(self):
        with (
            patch.object(youtube, "get_task", return_value={"status": "processing", "progress": 48}),
            patch.object(youtube, "_task_executor_is_alive", return_value=True),
            patch.object(
                youtube,
                "request_pause_task",
                return_value={"status": "pause_requested", "message": "Pausa solicitada."},
            ) as request_pause,
            patch.object(youtube, "_kick_story_video_task_queue_async") as kick,
        ):
            result = youtube.pause_task("task-running", _admin=SimpleNamespace(id=1))

        self.assertEqual(result["status"], "pause_requested")
        self.assertEqual(result["pause_mode"], "after_current_stage")
        self.assertTrue(result["assets_preserved"])
        self.assertFalse(request_pause.call_args.kwargs["immediate"])
        kick.assert_not_called()

    def test_pending_task_pauses_immediately_without_starting_paid_work(self):
        with (
            patch.object(youtube, "get_task", return_value={"status": "pending", "progress": 0}),
            patch.object(youtube, "_task_executor_is_alive", return_value=False),
            patch.object(
                youtube,
                "request_pause_task",
                return_value={"status": "paused", "message": "Pausada antes de iniciar."},
            ) as request_pause,
            patch.object(youtube, "_kick_story_video_task_queue_async") as kick,
        ):
            result = youtube.pause_task("task-pending", _admin=SimpleNamespace(id=1))

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["pause_mode"], "immediate")
        self.assertTrue(request_pause.call_args.kwargs["immediate"])
        kick.assert_called_once()

    def test_queue_contract_lists_occupiers_and_pause_controls(self):
        source = inspect.getsource(youtube.list_story_video_task_queue)
        self.assertIn('"occupiers"', source)
        self.assertIn('"paused_count"', source)
        self.assertIn('"queued_count"', source)
        self.assertIn("_load_production_video_queue_items", source)
        self.assertIn('"next_task"', source)


class ServerOccupancyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def test_busy_message_has_explicit_occupier_panel(self):
        self.assertIn("Servidor ocupado por {{ ytServerOccupiers.length }}", self.html)
        self.assertIn("Pausar após esta etapa", self.html)
        self.assertIn("Cancelar e liberar fila", self.html)
        self.assertIn("Os arquivos já gerados serão preservados", self.html)

    def test_ui_supports_pause_resume_and_hides_unknown_zero_resources(self):
        self.assertIn("async pauseQueuedVideoTask(item)", self.html)
        self.assertIn("async resumeQueuedVideoTask(item)", self.html)
        self.assertIn("Object.keys(ytStoryTask.runtime.resources).length", self.html)
        self.assertIn("if (status === 'paused')", self.html)
        self.assertIn("Retomar tarefa", self.html)
        self.assertIn("Fila de produção do servidor", self.html)
        self.assertIn("Pausar na fila", self.html)
        self.assertIn("ytQueueCounts.paused", self.html)

    def test_pause_uses_existing_queue_endpoints_only(self):
        pause_method = self.html.split("async pauseQueuedVideoTask(item)", 1)[1].split(
            "async resumeQueuedVideoTask(item)", 1
        )[0]
        self.assertIn("/youtube/task/${encodeURIComponent(taskId)}/pause", pause_method)
        self.assertIn("/youtube/videos/${productionVideoId}/pause", pause_method)
        self.assertNotIn("openai", pause_method.lower())
        self.assertNotIn("generate", pause_method.lower())


if __name__ == "__main__":
    unittest.main()
