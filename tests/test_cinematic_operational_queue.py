import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.routers.cinematic_queue import _bucket, _task_to_public
from app.services.cinematic_library_store import CinematicLibraryStore


class CinematicOperationalQueueTests(unittest.TestCase):
    def test_serializes_registered_video_task_for_v2(self):
        row = SimpleNamespace(
            id="task-123",
            user_id=7,
            status="awaiting_review",
            progress=100,
            message="Vídeo gerado e aguardando revisão.",
            stage="validation_complete",
            result_json=json.dumps({
                "payload": {
                    "topic": "Escute isto quando sua mente não consegue parar",
                    "duration": 10,
                    "kind": "devotional",
                },
                "video_url": "/media/videos/task-123.mp4",
            }),
            created_at=None,
            updated_at=None,
        )
        entry = {
            "task_id": "task-123",
            "origin": "cinematic_v2",
            "title": "Devocional V2",
            "project_slot": "devotional",
            "duration_minutes": 10,
            "registered_at": "2026-09-17T12:00:00+00:00",
        }
        item = _task_to_public(row, entry)
        self.assertEqual(item["id"], "task-123")
        self.assertEqual(item["title"], "Devocional V2")
        self.assertEqual(item["status_group"], "ready")
        self.assertEqual(item["progress"], 100)
        self.assertEqual(item["duration_minutes"], 10)
        self.assertEqual(item["origin"], "cinematic_v2")
        self.assertTrue(item["can_watch"])
        self.assertEqual(item["video_url"], "/media/videos/task-123.mp4")
        self.assertTrue(item["can_delete"])
        self.assertTrue(item["can_approve"])
        self.assertTrue(item["can_reject"])
        self.assertFalse(item["can_publish"])

    def test_status_buckets_are_professional_filters(self):
        self.assertEqual(_bucket("processing"), "active")
        self.assertEqual(_bucket("paused"), "active")
        self.assertEqual(_bucket("awaiting_review"), "ready")
        self.assertEqual(_bucket("published"), "ready")
        self.assertEqual(_bucket("failed"), "failed")
        self.assertEqual(_bucket("cancelled"), "cancelled")

    def test_library_store_is_explicit_v2_membership_and_soft_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CinematicLibraryStore(tmp)
            registered = store.register(
                9,
                "task-v2",
                title="Projeto novo",
                project_slot="story",
                duration_minutes=10,
            )
            self.assertEqual(registered["origin"], "cinematic_v2")
            self.assertEqual([item["task_id"] for item in store.list(9)], ["task-v2"])
            self.assertTrue(store.soft_delete(9, "task-v2"))
            self.assertEqual(store.list(9), [])
            self.assertEqual(store.get(9, "task-v2"), None)
            self.assertIsNotNone(store.get(9, "task-v2", include_deleted=True))
            self.assertTrue(store.restore(9, "task-v2"))
            self.assertEqual(len(store.list(9)), 1)

    def test_v2_queue_frontend_has_search_filters_pagination_and_project_actions(self):
        script = Path("app/static/operational_queue.js").read_text(encoding="utf-8")
        self.assertIn("page_size", script)
        self.assertIn("v2ProjectSearch", script)
        self.assertIn("data-filter=\"active\"", script)
        self.assertIn("data-filter=\"ready\"", script)
        self.assertIn("Visualizar", script)
        self.assertIn("Assistir", script)
        self.assertIn("Excluir", script)
        self.assertIn("/youtube/cinematic/queue/${encodeURIComponent(id)}", script)
        self.assertIn("method: 'DELETE'", script)
        self.assertIn("/youtube/task/${encodeURIComponent(id)}/${action}", script)
        self.assertIn("Atualização automática a cada 10 segundos", script)
        self.assertIn("oq-project-scroll", script)
        self.assertIn("overflow-x: scroll", script)
        self.assertIn("Deslize para o lado para ver progresso e comandos", script)
        self.assertIn("oq-project-row { min-width: 760px; }", script)
        self.assertIn("reconcileActiveProjects", script)
        self.assertIn("/youtube/task/${encodeURIComponent(task.id)}", script)
        self.assertIn("converted to a recoverable pause", script)
        self.assertIn("function apiErrorText(payload", script)
        self.assertIn("value.msg", script)
        self.assertIn("value.reason", script)
        self.assertIn("text === '[object Object]' ? '' : text", script)
        self.assertIn("throw new Error(apiErrorText(data", script)
        self.assertNotIn("throw new Error(data.detail || data.message", script)
        self.assertIn("confirmedRetryUrl", script)
        self.assertIn("/retry-plan", script)
        self.assertIn("optimization_plan_hash", script)
        self.assertIn("Novas chamadas pagas de imagem: 0", script)
        self.assertIn("CORREÇÃO DE QUALIDADE VISUAL", script)
        self.assertIn("Nova meta visual", script)
        self.assertIn("Novas imagens necessárias", script)
        self.assertIn("CUSTO PREVENTIVO ANTES DE AUTORIZAR", script)
        self.assertIn("Custo adicional máximo estimado desta correção", script)
        self.assertIn("Máximo de ${newCalls} novas chamadas pagas de imagem", script)
        self.assertIn("inclusive em retry", script)
        self.assertIn("✓ Aprovar", script)
        self.assertIn("Solicitar correção", script)
        self.assertIn("Publicar no YouTube", script)
        self.assertIn("reviewProject", script)
        self.assertIn("publishProject", script)
        self.assertIn("/publish`,", script)
        self.assertNotIn("queue?limit=50", script)

        router = Path("app/routers/cinematic_queue.py").read_text(encoding="utf-8")
        self.assertIn('@router.post("/queue/{task_id}/approve")', router)
        self.assertIn('@router.post("/queue/{task_id}/reject")', router)
        self.assertIn('@router.post("/queue/{task_id}/publish")', router)
        self.assertIn("director_quality_validation_failed", router)
        self.assertIn("minimum_visual_count_for_duration", router)
        self.assertIn('"strict_visual_quality_required": True', router)
        self.assertIn('"strict_visual_target_count"', router)

    def test_v2_handoff_registers_only_new_ui_tasks_in_library(self):
        script = Path("app/static/director_duration_contract.js").read_text(encoding="utf-8")
        self.assertIn("/youtube/cinematic/library/register", script)
        self.assertIn("codexia_v2_pending_library_tasks_v1", script)
        self.assertIn("project_slot", script)
        self.assertIn("registerLibraryTask", script)
        self.assertIn("director_quality_required: true", script)

    def test_ui_patch_bumps_queue_and_handoff_cache_versions(self):
        patch = Path("app/services/cinematic_ui_patch.py").read_text(encoding="utf-8")
        self.assertIn("operational_queue.js?v=20260919-cost-cap1", patch)
        self.assertIn("director_duration_contract.js?v=20260919-quality3", patch)
        self.assertIn("OPERATIONAL_QUEUE_SCRIPT_TAG", patch)
        self.assertIn("DURATION_CONTRACT_SCRIPT_TAG", patch)


if __name__ == "__main__":
    unittest.main()
