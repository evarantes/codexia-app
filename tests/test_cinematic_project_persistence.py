import json
from pathlib import Path

from app.services.cinematic_director_job_store import DirectorJobStore
from app.services.cinematic_project_store import CinematicProjectStore


def test_project_store_survives_reopen_and_merges_scene(tmp_path: Path):
    root = tmp_path / "projects"
    store = CinematicProjectStore(root=root)
    saved = store.write(7, {
        "theme": "Davi e Golias",
        "plan": {"theme": "Davi e Golias", "scenes": [{"index": 1}]},
    })
    assert saved["theme"] == "Davi e Golias"

    store.update_scene(7, 1, {"provider": "veo", "job_id": "job-123", "status": "PENDING"})
    reopened = CinematicProjectStore(root=root).read(7)
    assert reopened is not None
    assert reopened["plan"]["theme"] == "Davi e Golias"
    assert reopened["scenes"]["1"]["job_id"] == "job-123"

    CinematicProjectStore(root=root).update_scene(7, 1, {"status": "SUCCEEDED", "output_url": "/videos/pilot.mp4"})
    final = store.read(7)
    assert final["scenes"]["1"]["provider"] == "veo"
    assert final["scenes"]["1"]["status"] == "SUCCEEDED"
    assert final["scenes"]["1"]["output_url"] == "/videos/pilot.mp4"


def test_latest_completed_director_job_can_recover_paid_plan(tmp_path: Path):
    store = DirectorJobStore(root=tmp_path / "director")
    first, _ = store.create_if_absent(3, "job-old-1234", {"theme": "Antigo"})
    store.update(3, first["job_id"], status="completed", updated_at="2026-09-14T10:00:00+00:00", result={"plan": {"theme": "Antigo"}})
    second, _ = store.create_if_absent(3, "job-new-5678", {"theme": "Davi e Golias"})
    store.update(3, second["job_id"], status="completed", updated_at="2026-09-15T10:00:00+00:00", result={"plan": {"theme": "Davi e Golias"}})

    latest = DirectorJobStore(root=tmp_path / "director").latest_completed(3)
    assert latest is not None
    assert latest["job_id"] == "job-new-5678"
    assert latest["result"]["plan"]["theme"] == "Davi e Golias"
