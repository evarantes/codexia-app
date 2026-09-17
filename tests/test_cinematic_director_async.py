import json
import tempfile
import unittest
from pathlib import Path

from app.services.cinematic_director_job_store import DirectorJobStore
from app.services.cinematic_ui_patch import MOBILE_NAV_SCRIPT_TAG, SCRIPT_TAG, install_cinematic_async_ui


class DirectorJobStoreTests(unittest.TestCase):
    def test_same_request_id_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DirectorJobStore(tmp)
            first, created_first = store.create_if_absent(
                7,
                "job-12345678",
                {"theme": "Davi e Golias", "duration_minutes": 10},
            )
            second, created_second = store.create_if_absent(
                7,
                "job-12345678",
                {"theme": "OUTRO TEMA", "duration_minutes": 15},
            )
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(second["request"]["theme"], "Davi e Golias")

    def test_job_survives_store_reinstantiation(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_store = DirectorJobStore(tmp)
            first_store.create_if_absent(3, "persist-12345678", {"theme": "Daniel"})
            first_store.update(
                3,
                "persist-12345678",
                status="completed",
                progress=100,
                result={"plan": {"theme": "Daniel"}},
            )
            second_store = DirectorJobStore(tmp)
            recovered = second_store.read(3, "persist-12345678")
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["result"]["plan"]["theme"], "Daniel")

    def test_invalid_job_id_cannot_escape_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DirectorJobStore(tmp)
            with self.assertRaises(ValueError):
                store.create_if_absent(1, "../../segredo", {"theme": "x"})


class CinematicAsyncUiTests(unittest.TestCase):
    def test_ui_patch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text("<html><body><h1>Codexia</h1></body></html>", encoding="utf-8")
            self.assertTrue(install_cinematic_async_ui(index))
            self.assertFalse(install_cinematic_async_ui(index))
            html = index.read_text(encoding="utf-8")
            self.assertEqual(html.count(SCRIPT_TAG), 1)
            self.assertEqual(html.count(MOBILE_NAV_SCRIPT_TAG), 1)

    def test_mobile_navigation_controller_has_accessible_drawer(self):
        script = Path("app/static/mobile_nav.js").read_text(encoding="utf-8")
        self.assertIn("mobile-nav-toggle", script)
        self.assertIn("mobile-nav-open", script)
        self.assertIn("Abrir menu", script)
        self.assertIn("Fechar menu", script)
        self.assertIn("max-width:700px", script)
        self.assertIn("event.target.closest('button[data-page],a')", script)

    def test_async_controller_persists_job_before_post_and_polls(self):
        script = Path("app/static/cinematic_async_director.js").read_text(encoding="utf-8")
        self.assertIn("storeJob(id); // persist before POST", script)
        self.assertIn("/youtube/cinematic/director/jobs", script)
        self.assertIn("Retomar direção", script)
        self.assertIn("sem nova cobrança", script)

    def test_async_router_exposes_submit_and_status_endpoints(self):
        source = Path("app/routers/cinematic_director_async.py").read_text(encoding="utf-8")
        self.assertIn('@router.post("/director/jobs")', source)
        self.assertIn('@router.get("/director/jobs/{job_id}")', source)
        self.assertIn("create_if_absent", source)
        self.assertIn("threading.Thread", source)


if __name__ == "__main__":
    unittest.main()
