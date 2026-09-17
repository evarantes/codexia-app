import tempfile
import unittest
from pathlib import Path

from app.services.cinematic_director_job_store import DirectorJobStore
from app.services.cinematic_project_store import CinematicProjectStore


class CinematicProjectPersistenceTests(unittest.TestCase):
    def test_project_store_survives_reopen_and_merges_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            store = CinematicProjectStore(root=root)
            saved = store.write(7, {
                "theme": "Davi e Golias",
                "plan": {"theme": "Davi e Golias", "scenes": [{"index": 1}]},
            })
            self.assertEqual(saved["theme"], "Davi e Golias")

            store.update_scene(7, 1, {"provider": "veo", "job_id": "job-123", "status": "PENDING"})
            reopened = CinematicProjectStore(root=root).read(7)
            self.assertIsNotNone(reopened)
            self.assertEqual(reopened["plan"]["theme"], "Davi e Golias")
            self.assertEqual(reopened["scenes"]["1"]["job_id"], "job-123")

            CinematicProjectStore(root=root).update_scene(7, 1, {"status": "SUCCEEDED", "output_url": "/videos/pilot.mp4"})
            final = store.read(7)
            self.assertEqual(final["scenes"]["1"]["provider"], "veo")
            self.assertEqual(final["scenes"]["1"]["status"], "SUCCEEDED")
            self.assertEqual(final["scenes"]["1"]["output_url"], "/videos/pilot.mp4")

    def test_story_and_devotional_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            store = CinematicProjectStore(root=root)
            store.write(9, {"theme": "Davi e Golias", "content_type": "story"}, slot="story")
            store.write(9, {"theme": "Mente não consegue parar", "content_type": "devotional"}, slot="devotional")
            store.update_scene(9, 1, {"job_id": "story-job", "status": "PENDING"}, slot="story")
            store.update_scene(9, 1, {"job_id": "devotional-job", "status": "PENDING"}, slot="devotional")

            story = store.read(9, "story")
            devotional = store.read(9, "devotional")
            self.assertEqual(story["theme"], "Davi e Golias")
            self.assertEqual(devotional["theme"], "Mente não consegue parar")
            self.assertEqual(story["scenes"]["1"]["job_id"], "story-job")
            self.assertEqual(devotional["scenes"]["1"]["job_id"], "devotional-job")
            self.assertEqual(story["project_slot"], "story")
            self.assertEqual(devotional["project_slot"], "devotional")

    def test_latest_completed_director_job_can_recover_paid_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "director"
            store = DirectorJobStore(root=root)
            first, _ = store.create_if_absent(3, "job-old-1234", {"theme": "Antigo", "content_type": "story"})
            store.update(3, first["job_id"], status="completed", result={"plan": {"theme": "Antigo", "content_type": "story"}})
            second, _ = store.create_if_absent(3, "job-new-5678", {"theme": "Davi e Golias", "content_type": "story"})
            store.update(3, second["job_id"], status="completed", result={"plan": {"theme": "Davi e Golias", "content_type": "story"}})
            third, _ = store.create_if_absent(3, "job-dev-9999", {"theme": "Ansiedade", "content_type": "devotional"})
            store.update(3, third["job_id"], status="completed", result={"plan": {"theme": "Ansiedade", "content_type": "devotional"}})

            latest = DirectorJobStore(root=root).latest_completed(3)
            story = DirectorJobStore(root=root).latest_completed(3, content_type="story")
            devotional = DirectorJobStore(root=root).latest_completed(3, content_type="devotional")
            self.assertEqual(latest["job_id"], "job-dev-9999")
            self.assertEqual(story["job_id"], "job-new-5678")
            self.assertEqual(devotional["job_id"], "job-dev-9999")

    def test_frontend_uses_slot_aware_endpoints(self):
        script = Path("app/static/cinematic_project_state.js").read_text(encoding="utf-8")
        self.assertIn("slot=${encodeURIComponent(activeSlot)}", script)
        self.assertIn("project_slot:activeSlot", script)
        self.assertIn("O projeto de Davi e Golias continua salvo", script)


if __name__ == "__main__":
    unittest.main()
