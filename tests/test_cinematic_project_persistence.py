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

    def test_scene_rejection_notes_and_history_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            store = CinematicProjectStore(root=root)
            store.write(9, {
                "theme": "Davi e Golias",
                "plan": {"theme": "Davi e Golias", "scenes": [{"index": 1}]},
            })
            history = [{
                "correction_notes": "Não mostrar Davi nesta cena.",
                "output_url": "/videos/old-pilot.mp4",
                "job_id": "veo-old-job",
            }]
            store.update_scene(9, 1, {
                "status": "REJECTED",
                "approved": False,
                "correction_notes": "Não mostrar Davi nesta cena.",
                "rejection_history": history,
            })

            reopened = CinematicProjectStore(root=root).read(9)
            self.assertIsNotNone(reopened)
            scene = reopened["scenes"]["1"]
            self.assertEqual(scene["status"], "REJECTED")
            self.assertEqual(scene["correction_notes"], "Não mostrar Davi nesta cena.")
            self.assertEqual(scene["rejection_history"][0]["output_url"], "/videos/old-pilot.mp4")

            # A new provider job may replace active output/job fields without
            # erasing the correction instruction or the version history.
            store.update_scene(9, 1, {"status": "PENDING", "job_id": "veo-new-job", "output_url": None})
            regenerated = CinematicProjectStore(root=root).read(9)["scenes"]["1"]
            self.assertEqual(regenerated["correction_notes"], "Não mostrar Davi nesta cena.")
            self.assertEqual(regenerated["rejection_history"][0]["job_id"], "veo-old-job")
            self.assertEqual(regenerated["job_id"], "veo-new-job")

    def test_latest_completed_director_job_can_recover_paid_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "director"
            store = DirectorJobStore(root=root)
            first, _ = store.create_if_absent(3, "job-old-1234", {"theme": "Antigo"})
            store.update(3, first["job_id"], status="completed", result={"plan": {"theme": "Antigo"}})
            second, _ = store.create_if_absent(3, "job-new-5678", {"theme": "Davi e Golias"})
            store.update(3, second["job_id"], status="completed", result={"plan": {"theme": "Davi e Golias"}})

            latest = DirectorJobStore(root=root).latest_completed(3)
            self.assertIsNotNone(latest)
            self.assertEqual(latest["job_id"], "job-new-5678")
            self.assertEqual(latest["result"]["plan"]["theme"], "Davi e Golias")


if __name__ == "__main__":
    unittest.main()
