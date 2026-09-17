import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.routers.cinematic_queue import _task_to_public


class CinematicOperationalQueueTests(unittest.TestCase):
    def test_serializes_canonical_video_task_for_v2(self):
        row = SimpleNamespace(
            id="task-123",
            status="failed",
            progress=13,
            message="Roteiro fora da tolerância editorial",
            result_json=json.dumps({
                "payload": {
                    "topic": "Escute isto quando sua mente não consegue parar",
                    "duration": 10,
                    "kind": "devotional",
                },
                "error": "Roteiro estimado acima do limite flexível",
            }),
            created_at=None,
            updated_at=None,
        )
        item = _task_to_public(row)
        self.assertEqual(item["id"], "task-123")
        self.assertEqual(item["title"], "Escute isto quando sua mente não consegue parar")
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["progress"], 13)
        self.assertEqual(item["duration_minutes"], 10)
        self.assertTrue(item["can_retry"])
        self.assertIn("limite", item["error"])

    def test_v2_queue_frontend_uses_native_endpoint_and_task_controls(self):
        script = Path("app/static/operational_queue.js").read_text(encoding="utf-8")
        self.assertIn("/youtube/cinematic/queue?limit=50", script)
        self.assertIn("/youtube/task/${encodeURIComponent(id)}/${action}", script)
        self.assertIn("Atualização automática a cada 10 segundos", script)
        self.assertIn("Reiniciar agora", script)

    def test_ui_patch_injects_queue_controller(self):
        patch = Path("app/services/cinematic_ui_patch.py").read_text(encoding="utf-8")
        self.assertIn("operational_queue.js", patch)
        self.assertIn("OPERATIONAL_QUEUE_SCRIPT_TAG", patch)


if __name__ == "__main__":
    unittest.main()
