import inspect
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
