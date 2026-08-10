import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/codexia_test")

from app.database import Base  # noqa: E402
from app.models import ScheduledVideo, SeriesEpisode, Tenant, User  # noqa: E402
from app.services.youtube_series_service import youtube_series_service  # noqa: E402


class YouTubeAutoAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="yt-auto-automation-"))
        self.db_path = self.temp_dir / "auto.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()

        tenant = Tenant(name="Tenant Auto", slug="tenant-auto")
        self.db.add(tenant)
        self.db.flush()
        self.user = User(
            tenant_id=tenant.id,
            email="auto@codexia.test",
            name="Auto Tester",
            hashed_password="hash",
            is_active=True,
            is_admin=True,
            role="admin",
        )
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_auto_approval_moves_episode_to_scheduled_without_manual_endpoint(self):
        now = datetime.utcnow()
        editorial_plan = [{
            "episode_number": 1,
            "planned_title": "Ep 1",
            "narrated_title": "Ep 1",
            "summary": "Resumo",
            "publication_datetime": (now + timedelta(minutes=2)).isoformat(),
            "production_datetime": (now - timedelta(minutes=1)).isoformat(),
            "duration_minutes": 2,
            "next_episode_hook": "Gancho",
        }]
        with patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            detail = youtube_series_service.create_series(self.db, user=self.user, payload={
                "name": "Auto Série",
                "main_theme": "Tema",
                "start_date": now.date().isoformat(),
                "end_date": (now.date() + timedelta(days=1)).isoformat(),
                "status": "active",
                "timezone": "UTC",
                "publication_time": "00:00",
                "production_time": "00:00",
                "production_lead_days": 1,
                "duration_minutes": 2,
                "visibility": "unlisted",
                "auto_approval": True,
                "editorial_plan": editorial_plan,
            })

        series_id = detail["id"]
        episode_id = detail["episodes"][0]["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        episode.status = "in_production"
        episode.task_id = "task-auto-1"
        self.db.commit()

        fake_task = {
            "status": "completed",
            "result": {
                "title": "Ep 1",
                "description": "Desc",
                "video_url": "/static/videos/ep1.mp4",
                "cost_control": {"estimated_cost": 0.3, "actual_cost": 0.2},
            },
        }
        with patch("app.services.youtube_series_service.get_task", return_value=fake_task), \
             patch("app.services.youtube_series_service.acquire_distributed_lock", return_value={"backend": "test"}), \
             patch("app.services.youtube_series_service.release_distributed_lock", return_value=None), \
             patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            youtube_series_service.sync_series_scheduler(self.db, now=now)
            youtube_series_service.sync_series_scheduler(self.db, now=now)
            result = youtube_series_service.get_series_detail(self.db, user=self.user, series_id=series_id)

        updated = next(ep for ep in result["episodes"] if ep["id"] == episode_id)
        self.assertEqual(updated["status"], "scheduled")
        self.assertIsNotNone(updated.get("scheduled_video_id"))
        scheduled = self.db.query(ScheduledVideo).filter(ScheduledVideo.id == int(updated["scheduled_video_id"])).first()
        self.assertTrue(bool(scheduled.auto_post))
        self.assertEqual(scheduled.status, "completed")

    def test_update_publication_state_sets_episode_published_fields(self):
        now = datetime.utcnow()
        with patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            detail = youtube_series_service.create_series(self.db, user=self.user, payload={
                "name": "Auto Pub",
                "main_theme": "Tema",
                "start_date": now.date().isoformat(),
                "end_date": (now.date() + timedelta(days=1)).isoformat(),
                "status": "active",
                "timezone": "UTC",
                "publication_time": "00:00",
                "production_time": "00:00",
                "production_lead_days": 1,
                "duration_minutes": 2,
                "visibility": "unlisted",
                "auto_approval": False,
            })
        episode_id = detail["episodes"][0]["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        scheduled = ScheduledVideo(
            user_id=int(self.user.id),
            theme="Tema",
            title="T",
            description="D",
            scheduled_for=now - timedelta(minutes=1),
            status="published",
            video_type="video",
            script_data="{}",
            video_url="/static/videos/local.mp4",
            progress=100,
            auto_post=True,
            youtube_video_id="yt123",
            uploaded_at=now,
        )
        self.db.add(scheduled)
        self.db.flush()
        episode.scheduled_video_id = int(scheduled.id)
        episode.status = "scheduled"
        self.db.commit()

        youtube_series_service.update_publication_state_from_schedule(self.db, scheduled_video_id=int(scheduled.id))
        refreshed = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        self.assertEqual(refreshed.status, "published")
        self.assertEqual(refreshed.youtube_video_id, "yt123")
        self.assertTrue(refreshed.youtube_url and "yt123" in refreshed.youtube_url)


if __name__ == "__main__":
    unittest.main()
